"""FastAPI application: chat endpoint, tiny web widget, admin endpoints."""
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import date
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .agent import ChatAgent
from .booking import Calendar
from .config import BASE_DIR, Settings
from .crm import CRM
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


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    settings = settings or Settings()
    init_db(settings.db_path)
    retriever = build_retriever(settings)
    agent = ChatAgent(settings, retriever, llm or build_llm(settings), CRM(settings.db_path, settings.crm_webhook_url,
                                         webhook_secret=settings.crm_webhook_secret),
                      Calendar(settings.db_path, settings.open_hour, settings.close_hour, settings.slot_minutes,
                               settings.max_booking_days))
    @asynccontextmanager
    async def lifespan(_app):
        yield
        agent.crm.flush()   # do not lose webhooks that are still being sent when the server stops

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
                "llm_chain": [p.name for p in agent.llm.providers]}

    @app.post("/chat")
    def chat(body: ChatIn, request: Request):
        check_rate(body.session_id)
        check_public_limits(request)
        return agent.handle(body.session_id, body.message)

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
