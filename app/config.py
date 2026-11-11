"""Settings, read from environment variables (see .env.example)."""
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Tiny .env reader so the project has no extra dependency."""
    env_file = BASE_DIR / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


@dataclass
class Settings:
    knowledge_dir: Path = BASE_DIR / "knowledge"
    db_path: str = str(BASE_DIR / "data" / "app.db")
    # Order of LLM providers. The first working one answers, the rest are fallbacks.
    llm_providers: list = field(default_factory=lambda: [
        p.strip() for p in os.getenv("LLM_PROVIDERS", "anthropic,openai,mock").split(",") if p.strip()
    ])
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    # Small, fast model for side jobs (rewriting the search query, translating system messages).
    anthropic_fast_model: str = os.getenv("ANTHROPIC_FAST_MODEL", "claude-haiku-5-5")
    openai_fast_model: str = os.getenv("OPENAI_FAST_MODEL", "gpt-4o-mini")
    # Rewrite the visitor's message into an English search query (handles other languages and follow-ups).
    query_rewrite: bool = os.getenv("QUERY_REWRITE", "1") == "1"
    llm_timeout: float = float(os.getenv("LLM_TIMEOUT", "30"))
    # CRM: "sqlite" keeps leads in the local database, "webhook" also posts to your CRM / n8n.
    crm_webhook_url: str = os.getenv("CRM_WEBHOOK_URL", "")
    crm_webhook_secret: str = os.getenv("CRM_WEBHOOK_SECRET", "")   # sent as the X-Webhook-Secret header
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    # HubSpot (free CRM): a "private app" access token turns every lead into a HubSpot contact.
    hubspot_token: str = os.getenv("HUBSPOT_TOKEN", "")
    hubspot_base_url: str = os.getenv("HUBSPOT_BASE_URL", "https://api.hubapi.com")
    hubspot_custom_props: bool = os.getenv("HUBSPOT_CUSTOM_PROPS", "1") == "1"
    # Google Calendar: the id (usually an e-mail address) of a calendar that is shared with the service account,
    # and the path of that service account's JSON key file. Both are needed to turn the integration on.
    google_calendar_id: str = os.getenv("GOOGLE_CALENDAR_ID", "")
    google_service_account_file: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    google_api_base: str = os.getenv("GOOGLE_API_BASE", "https://www.googleapis.com/calendar/v3")
    timezone: str = os.getenv("TIMEZONE", "Europe/Bratislava")   # the time zone of the opening hours
    # Booking
    open_hour: int = int(os.getenv("OPEN_HOUR", "9"))
    close_hour: int = int(os.getenv("CLOSE_HOUR", "17"))
    slot_minutes: int = int(os.getenv("SLOT_MINUTES", "30"))
    max_booking_days: int = int(os.getenv("MAX_BOOKING_DAYS", "180"))   # how far ahead a visit can be booked
    top_k: int = int(os.getenv("TOP_K", "3"))
    # Where the knowledge base is searched: "bm25" (in memory, no extra service) or "qdrant" (a vector database).
    retriever: str = os.getenv("RETRIEVER", "bm25").lower()
    qdrant_url: str = os.getenv("QDRANT_URL", "")
    qdrant_api_key: str = os.getenv("QDRANT_API_KEY", "")
    qdrant_path: str = os.getenv("QDRANT_PATH", "")
    # Protection of a public chat: messages per minute for one IP address, and a cap on all messages per day
    # (every message costs money on the model's API). 0 turns a limit off.
    rate_limit_ip: int = int(os.getenv("RATE_LIMIT_IP", "40"))
    daily_message_cap: int = int(os.getenv("DAILY_MESSAGE_CAP", "1000"))
    trust_proxy: bool = os.getenv("TRUST_PROXY", "0") == "1"   # read the client IP from X-Forwarded-For (behind Caddy/nginx)
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "knowledge")
    # Meaning-based search on top of BM25 (only with RETRIEVER=qdrant): "" (off), "fastembed" (local) or "openai".
    embedder: str = os.getenv("EMBEDDER", "").lower()
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "")
    dense_min_score: float = float(os.getenv("DENSE_MIN_SCORE", "0.6"))  # cosine; depends on the model, tune it
    # The services that can be booked. The model must use exactly these names.
    services: list = field(default_factory=lambda: [
        x.strip() for x in os.getenv(
            "SERVICES", "Check-up,Cleaning,Teeth whitening,Filling,Orthodontic consultation").split(",") if x.strip()
    ])
    business_name: str = os.getenv("BUSINESS_NAME", "Bright Smile Dental")
