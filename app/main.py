"""FastAPI application: chat endpoint, tiny web widget, admin endpoints."""
import hmac
import logging
from contextlib import asynccontextmanager
import os
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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


def create_app(settings: Settings | None = None, llm=None) -> FastAPI:
    settings = settings or Settings()
    init_db(settings.db_path)
    retriever = build_retriever(settings)
    agent = ChatAgent(settings, retriever, llm or build_llm(settings), CRM(settings.db_path, settings.crm_webhook_url,
                                         webhook_secret=settings.crm_webhook_secret),
                      Calendar(settings.db_path, settings.open_hour, settings.close_hour, settings.slot_minutes))
    @asynccontextmanager
    async def lifespan(_app):
        yield
        agent.crm.flush()   # do not lose webhooks that are still being sent when the server stops

    app = FastAPI(title="RAG chatbot demo", version="1.0", lifespan=lifespan)

    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    if origins:  # only needed when the widget is embedded on another website
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["POST", "GET"], allow_headers=["*"])

    hits: dict[str, deque] = defaultdict(deque)

    def check_rate(session_id: str) -> None:
        now, q = time.monotonic(), hits[session_id]
        while q and now - q[0] > RATE_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(429, "Too many messages. Please wait a moment.")
        q.append(now)

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
    def chat(body: ChatIn):
        check_rate(body.session_id)
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
