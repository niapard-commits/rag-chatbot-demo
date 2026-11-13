"""FastAPI application: chat endpoint, tiny web widget, admin endpoints."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date
import os
import time
from collections import defaultdict, deque

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .agent import ChatAgent
from .booking import Calendar
from .config import BASE_DIR, Settings
from .crm import CRM
from .gcal import GoogleCalendarClient, service_account_token_provider
from .hubspot import HubSpot
from . import twilio_whatsapp, whatsapp
from .db import init_db
from .llm import build_llm
from .rag.retriever import build_retriever

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

RATE_LIMIT = 20       # messages
RATE_WINDOW = 60.0    # seconds, per session


class ChatIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=1000)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The message is empty")
        return value


def build_google_calendar(settings: Settings):
    """The Google Calendar client, or None when it is not configured or the key file cannot be read.
    A mistake here must not stop the chatbot: it then works with the local calendar only."""
    if not (settings.google_calendar_id and settings.google_service_account_file):
        return None
    try:
        return GoogleCalendarClient(settings.google_calendar_id,
                                    service_account_token_provider(settings.google_service_account_file),
                                    settings.timezone, settings.google_api_base)
    except Exception as exc:
        logging.getLogger("chatbot").error("Google Calendar is switched off, the key file cannot be used: %s", exc)
        return None


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    settings = settings or Settings()
    init_db(settings.db_path)
    retriever = build_retriever(settings)
    hubspot = HubSpot(settings.hubspot_token, settings.hubspot_base_url,
                      custom_props=settings.hubspot_custom_props) if settings.hubspot_token else None
    agent = ChatAgent(settings, retriever, llm or build_llm(settings), CRM(settings.db_path, settings.crm_webhook_url,
                                         webhook_secret=settings.crm_webhook_secret, hubspot=hubspot),
                      Calendar(settings.db_path, settings.open_hour, settings.close_hour, settings.slot_minutes,
                               settings.max_booking_days, google=build_google_calendar(settings),
                               business_name=settings.business_name))
    @asynccontextmanager
    async def lifespan(_app):
        yield
        agent.crm.flush()   # do not lose webhooks that are still being sent when the server stops
        agent.calendar.flush()

    app = FastAPI(title="RAG chatbot demo", version="1.0", lifespan=lifespan)

    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if origins:  # only needed when the widget is embedded on another website
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET"], allow_headers=["*"])

    hits: dict[str, deque] = defaultdict(deque)
    ip_hits: dict[str, deque] = defaultdict(deque)
    daily = {"day": date.today(), "count": 0}

    def check_rate(session_id: str) -> None:
        now, q = time.monotonic(), hits[session_id]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(429, "Too many messages. Please wait a moment.")
        q.append(now)

    def client_ip(request: Request) -> str:
        if settings.trust_proxy:  # only safe when the app is reachable through the proxy alone
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def check_public_limits(request: Request) -> None:
        """Per-IP rate limit and a daily cap for the whole chat: a visitor who invents new session ids
        must not be able to run up the bill of the model's API."""
        if settings.rate_limit_ip:
            now, q = time.monotonic(), ip_hits[client_ip(request)]
            while q and now - q[0] > RATE_WINDOW:
                q.popleft()
            if len(q) >= settings.rate_limit_ip:
                raise HTTPException(429, "Too many messages from your network. Please wait a moment.")
            q.append(now)
        check_daily_cap()

    def check_daily_cap() -> None:
        if settings.daily_message_cap:
            if daily["day"] != date.today():
                daily["day"], daily["count"] = date.today(), 0
            if daily["count"] >= settings.daily_message_cap:
                raise HTTPException(429, "The demo has reached its daily message limit. Please come back tomorrow.")
            daily["count"] += 1

    def admin(x_admin_token: str = Header(default="")) -> None:
        if not settings.admin_token:
            raise HTTPException(404, "Admin endpoints are disabled (set ADMIN_TOKEN to enable)")
        if not hmac.compare_digest(x_admin_token, settings.admin_token):
            raise HTTPException(401, "Invalid admin token")

    @app.get("/health")
    def health():
        return {"status": "ok", "chunks": len(retriever.chunks), "retriever": getattr(getattr(retriever, "primary", retriever), "__class__").__name__,
                "search_degraded": getattr(retriever, "primary_active", True) is False,
                "llm_chain": [p.name for p in agent.llm.providers],
                "hubspot": agent.crm.hubspot is not None,
                "google_calendar": agent.calendar.google is not None,
                "google_calendar_degraded": agent.calendar.google_degraded}

    @app.post("/chat")
    def chat(body: ChatIn, request: Request):
        check_rate(body.session_id)
        check_public_limits(request)
        return agent.handle(body.session_id, body.message)

    wa = (whatsapp.WhatsAppChannel(settings.whatsapp_access_token, settings.whatsapp_phone_number_id,
                                   settings.whatsapp_api_base)
          if settings.whatsapp_verify_token and settings.whatsapp_access_token and settings.whatsapp_phone_number_id
          else None)
    app.state.whatsapp = wa

    def compose_reply(session_id: str, text: str | None, log_name: str, text_only: str, error_reply: str) -> str:
        """The chatbot's answer to one WhatsApp message. Never raises: a limit or a failure becomes the reply."""
        if text is None:
            return text_only
        try:
            check_rate(session_id)
            check_daily_cap()   # the same daily cap as the website: every message costs money on the model's API
            return agent.handle(session_id, text[:1000])["reply"]
        except HTTPException as exc:   # a limit: its message says what to do
            return str(exc.detail)
        except Exception:
            logging.getLogger(log_name).exception("Could not answer a WhatsApp message")
            return error_reply

    def answer_on_channel(channel, session_id: str, sender: str, text: str | None, log_name: str,
                          text_only: str, error_reply: str) -> None:
        """Runs after the webhook call was acknowledged (the provider wants an answer within seconds, the model needs longer)."""
        channel.send(sender, compose_reply(session_id, text, log_name, text_only, error_reply))

    def answer_on_whatsapp(sender: str, text: str | None) -> None:
        answer_on_channel(wa, whatsapp.session_id_for(sender), sender, text, "whatsapp",
                          whatsapp.TEXT_ONLY, whatsapp.ERROR_REPLY)

    if wa:
        @app.get("/whatsapp/webhook")
        def whatsapp_verify(request: Request):
            q = request.query_params   # Meta sends hub.mode, hub.verify_token and hub.challenge once, when the webhook is saved
            if q.get("hub.mode") == "subscribe" and hmac.compare_digest(q.get("hub.verify_token", ""),
                                                                         settings.whatsapp_verify_token):
                return PlainTextResponse(q.get("hub.challenge", ""))
            raise HTTPException(403, "Verification failed")

        @app.post("/whatsapp/webhook")
        async def whatsapp_incoming(request: Request, background: BackgroundTasks):
            raw = await request.body()
            if settings.whatsapp_app_secret and not whatsapp.signature_ok(
                    settings.whatsapp_app_secret, raw, request.headers.get("x-hub-signature-256", "")):
                raise HTTPException(401, "Bad signature")
            try:
                payload = await request.json()
            except ValueError:
                raise HTTPException(400, "Not JSON")
            for st in whatsapp.failed_statuses(payload):
                logging.getLogger("whatsapp").warning("WhatsApp could not deliver a message: %s", st)
            for m in whatsapp.extract_messages(payload):
                if m["from"] and wa.is_new(m["id"]):
                    background.add_task(answer_on_whatsapp, m["from"], m["text"])
            return {"ok": True}

    tw = (twilio_whatsapp.TwilioChannel(settings.twilio_account_sid, settings.twilio_auth_token,
                                        settings.twilio_whatsapp_from)
          if settings.twilio_account_sid and settings.twilio_auth_token and settings.twilio_webhook_url else None)
    app.state.twilio = tw

    if tw:
        @app.post("/twilio/whatsapp")
        async def twilio_incoming(request: Request):
            params = twilio_whatsapp.parse_form(await request.body())
            if not twilio_whatsapp.signature_ok(settings.twilio_auth_token, settings.twilio_webhook_url, params,
                                                request.headers.get("x-twilio-signature", "")):
                raise HTTPException(401, "Bad signature")
            xml = "application/xml"
            m = twilio_whatsapp.extract_message(params)
            if not m or not tw.is_new(m["id"]):   # not a WhatsApp message, or Twilio repeated a call we already took
                return Response(twilio_whatsapp.EMPTY_TWIML, media_type=xml)
            task = asyncio.ensure_future(run_in_threadpool(
                compose_reply, twilio_whatsapp.session_id_for(m["from"]), m["text"], "twilio",
                twilio_whatsapp.TEXT_ONLY, twilio_whatsapp.ERROR_REPLY))
            done, _ = await asyncio.wait({task}, timeout=settings.twilio_reply_wait)
            if task in done:   # the usual case: the answer travels back in this response
                return Response(twilio_whatsapp.twiml_reply(task.result()), media_type=xml)
            # too slow for Twilio's wait: acknowledge now, send the answer when it is ready
            loop = asyncio.get_running_loop()
            task.add_done_callback(lambda t: loop.run_in_executor(None, tw.send, m["from"], t.result()))
            return Response(twilio_whatsapp.EMPTY_TWIML, media_type=xml)

    @app.post("/admin/reindex", dependencies=[Depends(admin)])
    def reindex():
        return {"chunks": retriever.reindex()}

    @app.get("/admin/leads", dependencies=[Depends(admin)])
    def leads():
        return agent.crm.list_leads()

    @app.get("/")
    def index():
        return FileResponse(BASE_DIR / "static" / "index.html")

    app.state.agent = agent
    return app


app = create_app()
