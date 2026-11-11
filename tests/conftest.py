from datetime import datetime

import pytest

from app.agent import ChatAgent
from app.booking import Calendar
from app.config import Settings
from app.crm import CRM
from app.db import init_db
from app.llm import FallbackLLM, MockLLM
from app.rag.retriever import BM25Retriever

# Monday 2030-01-07, 08:00. A fixed "now" keeps booking tests independent of the real date.
NOW = datetime(2030, 1, 7, 8, 0)
TUESDAY = "2030-01-08"
SATURDAY = "2030-01-12"


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = str(tmp_path / "test.db")
    s.llm_providers = ["mock"]
    s.query_rewrite = False   # tests that need it switch it on
    init_db(s.db_path)
    return s


@pytest.fixture
def calendar(settings):
    return Calendar(settings.db_path)


@pytest.fixture
def crm(settings):
    return CRM(settings.db_path)


@pytest.fixture
def agent(settings, calendar, crm):
    return ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([MockLLM()]), crm, calendar)
