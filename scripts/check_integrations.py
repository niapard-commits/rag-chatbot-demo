"""Checks the HubSpot and Google Calendar settings from your .env and tells you what is wrong.

    python -m scripts.check_integrations                     # read-only checks
    python -m scripts.check_integrations --create-properties # also creates the 5 custom HubSpot contact properties
    python -m scripts.check_integrations --write-test        # also creates and removes one test contact / test event

Nothing secret is printed. Exit code 0 = everything that is configured works."""
import argparse
import sys
from datetime import date, datetime, timedelta

from app.config import Settings
from app.gcal import GoogleCalendarClient, service_account_token_provider
from app.http_json import HttpError
from app.hubspot import CUSTOM_PROPERTIES, HubSpot

OK, BAD, SKIP = "[ OK ]", "[FAIL]", "[skip]"


def explain_hubspot(exc: Exception) -> str:
    if isinstance(exc, HttpError):
        if exc.status == 401:
            return "HubSpot says the token is wrong or expired (HTTP 401). Create the private app token again."
        if exc.status == 403:
            return ("HubSpot says the token has no permission for this (HTTP 403). In the private app add the scopes "
                    "crm.objects.contacts.read, crm.objects.contacts.write, crm.schemas.contacts.read, "
                    f"crm.schemas.contacts.write. Details: {exc.body[:200]}")
    return str(exc)


def check_hubspot(settings: Settings, create: bool, write: bool) -> bool:
    if not settings.hubspot_token:
        print(f"{SKIP} HubSpot: HUBSPOT_TOKEN is empty, the integration is off.")
        return True
    hs = HubSpot(settings.hubspot_token, settings.hubspot_base_url, retry_delays=(1.0,))
    try:
        hs.check_access()
        print(f"{OK} HubSpot: the token works and can read contacts.")
        missing = hs.missing_properties()
    except Exception as exc:
        print(f"{BAD} HubSpot: {explain_hubspot(exc)}")
        return False
    good = True
    if missing and create:
        try:
            hs.create_properties(missing)
            missing = hs.missing_properties()
        except Exception as exc:
            print(f"{BAD} HubSpot: could not create the custom properties: {explain_hubspot(exc)}")
            good = False
    if missing:
        print(f"{BAD} HubSpot: these custom properties do not exist yet: {', '.join(missing)}. "
              f"Run again with --create-properties. (Without them only name, e-mail and phone are saved.)")
        good = False
    else:
        print(f"{OK} HubSpot: all {len(CUSTOM_PROPERTIES)} custom properties exist.")
    if write and good:
        try:
            contact_id = hs.sync_lead({"name": "Chatbot Test", "email": "chatbot.test@example.com", "service": "Check-up",
                                       "score": "cold", "score_reason": "Connection test", "note": "Created by check_integrations",
                                       "appointment": "test"})
            hs.archive_contact(contact_id)
            print(f"{OK} HubSpot: a test contact was written and removed again.")
        except Exception as exc:
            print(f"{BAD} HubSpot: the test write failed: {explain_hubspot(exc)}")
            good = False
    return good


def check_calendar(settings: Settings, write: bool) -> bool:
    if not (settings.google_calendar_id or settings.google_service_account_file):
        print(f"{SKIP} Google Calendar: GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_FILE are empty, the integration is off.")
        return True
    if not (settings.google_calendar_id and settings.google_service_account_file):
        print(f"{BAD} Google Calendar: set BOTH GOOGLE_CALENDAR_ID and GOOGLE_SERVICE_ACCOUNT_FILE.")
        return False
    try:
        token = service_account_token_provider(settings.google_service_account_file)
        token()
        print(f"{OK} Google Calendar: the key file works, Google gave an access token.")
    except FileNotFoundError:
        print(f"{BAD} Google Calendar: the key file {settings.google_service_account_file} was not found.")
        return False
    except Exception as exc:
        print(f"{BAD} Google Calendar: the key file could not be used: {exc}")
        return False
    client = GoogleCalendarClient(settings.google_calendar_id, token, settings.timezone, settings.google_api_base)
    day = date.today() + timedelta(days=1)
    try:
        busy = client.busy(day)
        print(f"{OK} Google Calendar: the calendar is readable ({len(busy)} busy period(s) tomorrow).")
    except Exception as exc:
        hint = ""
        if "notFound" in str(exc) or "404" in str(exc):
            hint = (" Share the calendar with the service account's e-mail (the client_email in the key file), "
                    "permission 'Make changes to events', and check GOOGLE_CALENDAR_ID.")
        elif "403" in str(exc) or "accessNotConfigured" in str(exc):
            hint = " Enable the Google Calendar API in the Google Cloud project of this service account."
        print(f"{BAD} Google Calendar: {exc}{hint}")
        return False
    if write:
        try:
            start = datetime(day.year, day.month, day.day, 23, 0)
            event_id = client.create_event("Chatbot connection test", "Created by check_integrations, deleted at once.",
                                           start, start + timedelta(minutes=30), "check")
            client.delete_event(event_id)
            print(f"{OK} Google Calendar: a test event was created and deleted again.")
        except Exception as exc:
            print(f"{BAD} Google Calendar: the test event failed: {exc} "
                  f"(the calendar must be shared with 'Make changes to events', not 'See all event details').")
            return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--create-properties", action="store_true", help="create the custom HubSpot contact properties")
    parser.add_argument("--write-test", action="store_true", help="create and remove one test contact and one test event")
    args = parser.parse_args(argv)
    settings = Settings()
    results = [check_hubspot(settings, args.create_properties, args.write_test),
               check_calendar(settings, args.write_test)]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
