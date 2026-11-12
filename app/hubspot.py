"""HubSpot CRM adapter (free CRM, "private app" access token).

Every lead of the chat becomes a HubSpot contact:
  * the contact is found by e-mail (upsert), so the same person never appears twice;
  * the id that HubSpot returns is stored in the local database, so the next update of the same chat is a plain PATCH;
  * a lead that has only a phone number is created without e-mail and then updated by that stored id.
Besides the standard fields (first name, last name, e-mail, phone) the bot writes five custom contact properties
(service, score, reason, note, appointment). They must exist in HubSpot: scripts/check_integrations.py --create-properties
creates them. If they are missing, the contact is still saved with the standard fields and a warning is logged once.

The access token needs the scopes crm.objects.contacts.read and crm.objects.contacts.write
(plus crm.schemas.contacts.write and crm.schemas.contacts.read only for creating the custom properties)."""
import logging

from .http_json import HttpError, request_json

log = logging.getLogger("chatbot.hubspot")

CUSTOM_PROPERTIES = {   # internal name -> label shown in HubSpot
    "chatbot_service": "Chatbot: service",
    "chatbot_score": "Chatbot: lead score",
    "chatbot_score_reason": "Chatbot: score reason",
    "chatbot_note": "Chatbot: note",
    "chatbot_appointment": "Chatbot: appointment",
}


class HubSpot:
    def __init__(self, token: str, base_url: str = "https://api.hubapi.com", timeout: float = 8.0,
                 custom_props: bool = True, retry_delays: tuple = (1.0, 3.0)):
        self.token, self.base_url, self.timeout = token, base_url.rstrip("/"), timeout
        self.custom_props, self.retry_delays = custom_props, retry_delays
        self._warned = False

    # ------------------------------------------------------------ low level
    def call(self, method: str, path: str, payload: dict | None = None):
        return request_json(method, self.base_url + path, {"Authorization": f"Bearer {self.token}"}, payload,
                            self.timeout, self.retry_delays)

    @staticmethod
    def split_name(name: str) -> tuple[str, str]:
        parts = (name or "").split(None, 1)
        return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")

    def _properties(self, lead: dict, with_custom: bool) -> dict:
        first, last = self.split_name(lead.get("name") or "")
        props = {"firstname": first, "lastname": last}
        if lead.get("phone"):
            props["phone"] = lead["phone"]
        if with_custom:
            for prop, key in (("chatbot_service", "service"), ("chatbot_score", "score"),
                              ("chatbot_score_reason", "score_reason"), ("chatbot_note", "note"),
                              ("chatbot_appointment", "appointment")):
                if lead.get(key):
                    props[prop] = lead[key]
        return props

    # ------------------------------------------------------------ the one operation the bot needs
    def sync_lead(self, lead: dict, contact_id: str | None = None) -> str | None:
        """Create or update the contact of this lead and return its HubSpot id.
        lead: name, email, phone, service, note, score, score_reason, appointment (all optional except name)."""
        with_custom = self.custom_props
        try:
            return self._sync(lead, contact_id, with_custom)
        except HttpError as exc:
            if with_custom and exc.status == 400 and "PROPERTY_DOESNT_EXIST" in exc.body:
                if not self._warned:
                    log.warning("HubSpot custom properties are missing, saving standard fields only. "
                                "Run: python -m scripts.check_integrations --create-properties")
                    self._warned = True
                return self._sync(lead, contact_id, False)
            raise

    def _sync(self, lead: dict, contact_id: str | None, with_custom: bool) -> str | None:
        props = self._properties(lead, with_custom)
        if contact_id:
            try:
                self.call("PATCH", f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
                return contact_id
            except HttpError as exc:
                if exc.status != 404:       # the contact was deleted in HubSpot: create it again below
                    raise
        email = lead.get("email")
        if email:
            # HubSpot does not support partial upserts by e-mail, so the e-mail is also sent as a property.
            body = {"inputs": [{"idProperty": "email", "id": email, "properties": {**props, "email": email}}]}
            answer = self.call("POST", "/crm/v3/objects/contacts/batch/upsert", body)
            return str(answer["results"][0]["id"])
        answer = self.call("POST", "/crm/v3/objects/contacts", {"properties": props})
        return str(answer["id"])

    # ------------------------------------------------------------ used by scripts/check_integrations.py
    def check_access(self) -> None:
        self.call("GET", "/crm/v3/objects/contacts?limit=1")

    def missing_properties(self) -> list[str]:
        missing = []
        for name in CUSTOM_PROPERTIES:
            try:
                self.call("GET", f"/crm/v3/properties/contacts/{name}")
            except HttpError as exc:
                if exc.status != 404:
                    raise
                missing.append(name)
        return missing

    def create_properties(self, names: list[str]) -> None:
        for name in names:
            body = {"name": name, "label": CUSTOM_PROPERTIES[name], "type": "string", "fieldType": "text",
                    "groupName": "contactinformation"}
            try:
                self.call("POST", "/crm/v3/properties/contacts", body)
            except HttpError as exc:
                if exc.status != 409:       # 409 = already exists
                    raise

    def archive_contact(self, contact_id: str) -> None:
        self.call("DELETE", f"/crm/v3/objects/contacts/{contact_id}")
