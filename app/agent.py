"""The chat agent: retrieve knowledge -> ask the LLM -> validate what it extracted -> save lead / book -> reply.

The LLM only talks and extracts data. Everything with consequences (saving a lead, booking a slot) is done by
plain code after validation, so a wrong or invented model answer can never create a booking by itself.
"""
import json
import logging
import re
from collections import OrderedDict
from datetime import date, datetime

from .booking import BookingError, Calendar, normalize_time
from .config import Settings
from .crm import CRM
from .llm import FallbackLLM, LLMError
from .prompts import REWRITE_SYSTEM, TRANSLATE_SYSTEM, build_system
from .rag.retriever import BM25Retriever

log = logging.getLogger("chatbot.agent")

MAX_MESSAGE_CHARS = 1000
LEAD_SCORES = ("hot", "warm", "cold")
MAX_HISTORY = 8
MAX_SESSIONS = 500
MAX_CONTEXT_CHUNKS = 14
EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
FALLBACK_REPLY = ("Sorry, I am having technical trouble right now. Please try again in a minute "
                  "or leave your name and phone number and our team will call you back.")


def parse_model_output(raw: str) -> dict:
    """Return {"reply", "intent", "fields"} even if the model wrapped the JSON in text or code fences."""
    text = raw.strip()
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:  # take the first valid JSON object that has a reply, even if there is text around it
        try:
            data, _ = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(data, dict) and isinstance(data.get("reply"), str) and data["reply"].strip():
            fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
            intent = data.get("intent") if data.get("intent") in ("question", "lead", "book") else "question"
            return {"reply": data["reply"].strip(), "intent": intent, "fields": fields}
        pos = text.find("{", pos + 1)
    # The model ignored the format: use the text before any JSON as the reply and extract nothing.
    plain = re.sub(r"```(?:json)?", "", text.split("{", 1)[0]).strip()
    return {"reply": plain or FALLBACK_REPLY, "intent": "question", "fields": {}}


def clean_fields(fields: dict, services: list[str] | None = None) -> dict:
    """Keep only values that look valid. Anything else is dropped, so the bot asks again.
    If `services` is given, "service" must be one of them (case-insensitive) and is stored with the canonical spelling."""
    out = {}
    name = fields.get("name")
    if isinstance(name, str) and 1 < len(name.strip()) <= 60:
        out["name"] = name.strip()
    email = fields.get("email")
    if isinstance(email, str) and EMAIL_RE.match(email.strip()):
        out["email"] = email.strip()
    phone = fields.get("phone")
    if isinstance(phone, str) and 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        out["phone"] = phone.strip()
    service = fields.get("service")
    if isinstance(service, str) and 1 < len(service.strip()) <= 60:
        if services is None:
            out["service"] = service.strip()
        else:
            canonical = {x.lower(): x for x in services}
            if service.strip().lower() in canonical:
                out["service"] = canonical[service.strip().lower()]
    lang = fields.get("language")
    if isinstance(lang, str) and re.fullmatch(r"[A-Za-z]{2,3}", lang.strip()):
        out["language"] = lang.strip().lower()
    note = fields.get("note")
    if isinstance(note, str) and note.strip():
        out["note"] = note.strip()[:300]
    score = fields.get("score")
    if isinstance(score, str) and score.strip().lower() in LEAD_SCORES:
        out["score"] = score.strip().lower()
        reason = fields.get("score_reason")
        if isinstance(reason, str) and reason.strip():
            out["score_reason"] = reason.strip()[:200]
    day = fields.get("date")
    if isinstance(day, str):
        try:
            out["date"] = date.fromisoformat(day.strip()).isoformat()
        except ValueError:
            pass
    slot = fields.get("time")
    if isinstance(slot, str):
        try:
            out["time"] = normalize_time(slot)
        except ValueError:
            pass
    return out


class ChatAgent:
    def __init__(self, settings: Settings, retriever: BM25Retriever, llm: FallbackLLM, crm: CRM, calendar: Calendar):
        self.settings, self.retriever, self.llm, self.crm, self.calendar = settings, retriever, llm, crm, calendar
        self.sessions: OrderedDict[str, dict] = OrderedDict()

    # ------------------------------------------------------------ sessions
    def _session(self, session_id: str) -> dict:
        if session_id in self.sessions:
            self.sessions.move_to_end(session_id)
        else:
            self.sessions[session_id] = {"state": {}, "history": [], "chunks": []}
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
        return self.sessions[session_id]

    # ------------------------------------------------------------ main entry
    def handle(self, session_id: str, message: str, now: datetime | None = None) -> dict:
        message = (message or "").strip()
        if not message:
            raise ValueError("Empty message")
        message = message[:MAX_MESSAGE_CHARS]
        sess = self._session(session_id)
        state = sess["state"]

        chunks = self.retriever.search(message, k=self.settings.top_k)
        # The model rewrites the query only when it helps: other languages, no hit, or a very short follow-up.
        # (An extra model call costs 1-2 seconds, so a plain English question that already matched skips it.)
        if self.settings.query_rewrite and (not message.isascii() or not chunks or len(message.split()) <= 3):
            chunks = self.retriever.search(self._search_query(sess, message), k=self.settings.top_k)
        # Keep what was retrieved for earlier questions too. Without this the model sees its own earlier
        # answers but not their source, and starts "correcting" them (found in a live test).
        context = chunks + [c for c in sess["chunks"] if c not in chunks]
        sess["chunks"] = context[:MAX_CONTEXT_CHUNKS]
        system = build_system(self.settings.business_name, self._public_state(state),
                              [c.as_context() for c in sess["chunks"]], self.settings.services,
                              today=(now or datetime.now()).date())
        messages = sess["history"][-MAX_HISTORY:] + [{"role": "user", "content": message}]

        try:
            parsed = parse_model_output(self.llm.complete(system, messages))
        except LLMError as exc:
            log.error("All LLM providers failed: %s", exc)
            return {"reply": FALLBACK_REPLY, "state": self._public_state(state), "sources": [],
                    "provider": None, "booking": None}

        confirm = parsed["fields"].get("confirm") if isinstance(parsed["fields"], dict) else None
        state.update(clean_fields(parsed["fields"], self.settings.services))
        if state.get("_booked_reason"):  # once a visitor has booked, the model cannot lower the score again
            state["score"], state["score_reason"] = "hot", state["_booked_reason"]
        if parsed["intent"] == "book":
            state["booking_started"] = True

        reply, booking = parsed["reply"], None
        self._maybe_save_lead(session_id, state)
        booking_reply = self._maybe_book(session_id, state, now, confirm)
        if booking_reply:
            reply, booking = self._localize(booking_reply[0], state.get("language")), booking_reply[1]

        sess["history"] += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        sess["history"] = sess["history"][-2 * MAX_HISTORY:]
        # Show sources only when the visitor asked a question (not while collecting booking details).
        sources = [f"{c.source}: {c.title}" for c in chunks] if parsed["intent"] == "question" else []
        return {"reply": reply, "state": self._public_state(state), "provider": self.llm.last_used,
                "sources": sources, "booking": booking}

    # ------------------------------------------------------------ helpers around the model
    def _search_query(self, sess: dict, message: str) -> str:
        """English search query for the knowledge base. Fixes two problems found in live tests:
        questions in other languages found nothing, and follow-ups like "and how long does it take?" lacked context."""
        try:
            history = sess["history"][-4:]
            rewritten = self.llm.complete(REWRITE_SYSTEM.format(business=self.settings.business_name),
                                          history + [{"role": "user", "content": message}], structured=False)
            return (rewritten.strip().splitlines() or [""])[0][:200] + " " + message
        except LLMError as exc:
            log.warning("Query rewrite failed, searching with the original message: %s", exc)
            return message

    def _localize(self, text: str, lang: str | None) -> str:
        """Messages written by our own code are English. Translate them into the visitor's language."""
        if not lang or lang == "en":
            return text
        try:
            return self.llm.complete(TRANSLATE_SYSTEM.format(business=self.settings.business_name, lang=lang,
                                                      services=", ".join(self.settings.services)),
                              [{"role": "user", "content": text}],
                                     structured=False).strip() or text
        except LLMError as exc:
            log.warning("Translation failed, sending English text: %s", exc)
            return text

    # ------------------------------------------------------------ actions
    def _maybe_save_lead(self, session_id: str, state: dict) -> None:
        if not (state.get("name") and (state.get("email") or state.get("phone"))):
            return
        signature = (state.get("name"), state.get("email"), state.get("phone"), state.get("service"), state.get("note"),
                     state.get("score"), state.get("score_reason"))
        if state.get("_lead_sig") == signature:
            return
        self.crm.save_lead(session_id, state["name"], state.get("email"), state.get("phone"),
                           state.get("service"), state.get("note"), state.get("score"), state.get("score_reason"))
        state["_lead_sig"] = signature

    def _maybe_book(self, session_id: str, state: dict, now: datetime | None, confirm=None):
        if not state.get("booking_started"):
            return None
        contact = state.get("email") or state.get("phone")
        if not (state.get("service") and state.get("date") and state.get("time") and state.get("name") and contact):
            return None
        # Never book straight away: the model may have misread a relative date ("next Thursday").
        # The code shows its own summary and books only after an explicit yes about exactly these details.
        signature = (state["service"], state["date"], state["time"], state["name"], contact)
        if state.get("_pending") == signature and confirm is False:
            state.pop("_pending", None)
            state.pop("awaiting_confirmation", None)
            state.pop("date", None)
            state.pop("time", None)
            return "No problem, I have not booked anything. Which day and time would suit you instead?", None
        if state.get("_pending") != signature or confirm is not True:
            state["_pending"] = signature
            weekday = date.fromisoformat(state["date"]).strftime("%A")
            summary = f"{state['service']} on {weekday} {state['date']} at {state['time']} for {state['name']} ({contact})"
            state["awaiting_confirmation"] = summary
            return f"Please check: {summary}. Shall I book it? (yes / no)", None
        state.pop("_pending", None)
        state.pop("awaiting_confirmation", None)
        try:
            b = self.calendar.book(session_id=session_id, name=state["name"], contact=contact,
                                   service=state["service"], day=state["date"], slot=state["time"], now=now)
        except BookingError as exc:
            # Forget the rejected date/time so the visitor can choose another one.
            state.pop("date", None)
            state.pop("time", None)
            return str(exc) + " Which other day and time would you prefer?", None
        self.crm.notify_booking(session_id, state["name"], contact, b.service, b.day, b.slot)
        # A confirmed booking is the strongest buying signal there is: the code sets the score, not the model.
        state["_booked_reason"] = f"Booked {b.service} on {b.day} at {b.slot}."
        state["score"], state["score_reason"] = "hot", state["_booked_reason"]
        self._maybe_save_lead(session_id, state)
        for key in ("service", "date", "time", "booking_started"):
            state.pop(key, None)
        info = {"id": b.id, "service": b.service, "day": b.day, "time": b.slot}
        return (f"Your appointment is booked: {b.service} on {b.day} at {b.slot}. "
                f"We will contact you at {contact} if anything changes. Anything else I can help with?"), info

    @staticmethod
    def _public_state(state: dict) -> dict:
        return {k: v for k, v in state.items() if not k.startswith("_")}
