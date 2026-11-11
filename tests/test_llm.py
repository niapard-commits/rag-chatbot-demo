import pytest

from app.llm import FallbackLLM, LLMError, MockLLM


class Broken:
    name = "broken"

    def complete(self, system, messages, structured=True):
        raise TimeoutError("provider down")


class Blank:
    name = "blank"

    def complete(self, system, messages, structured=True):
        return "   "


class Fixed:
    name = "fixed"

    def complete(self, system, messages, structured=True):
        return "ok"


def test_falls_back_to_the_next_provider():
    llm = FallbackLLM([Broken(), Fixed()])
    assert llm.complete("s", [{"role": "user", "content": "hi"}]) == "ok"
    assert llm.last_used == "fixed"


def test_empty_answer_counts_as_failure():
    llm = FallbackLLM([Blank(), Fixed()])
    assert llm.complete("s", []) == "ok"


def test_primary_is_used_when_it_works():
    llm = FallbackLLM([Fixed(), MockLLM()])
    llm.complete("s", [])
    assert llm.last_used == "fixed"


def test_all_failing_raises_llm_error():
    with pytest.raises(LLMError, match="All providers failed"):
        FallbackLLM([Broken(), Broken()]).complete("s", [])
