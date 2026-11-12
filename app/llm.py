"""LLM providers with automatic fallback: Claude -> OpenAI -> (offline) mock.

Every provider has one method: complete(system, messages) -> str.
FallbackLLM tries them in order and moves on when one fails (rate limit, outage, timeout).
"""
import json
import logging
import re
from datetime import date, timedelta
from typing import Protocol

from .config import Settings

log = logging.getLogger("chatbot.llm")


class LLMError(Exception):
    pass


class LLM(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], structured: bool = True) -> str: ...


_NULLABLE_STR = {"type": ["string", "null"]}
REPLY_TOOL = {
    "name": "reply_to_visitor",
    "description": "Send the answer to the visitor and report the data you extracted from the conversation.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {"type": "string"},
            "intent": {"type": "string", "enum": ["question", "lead", "book", "change"]},
            "fields": {
                "type": "object",
                "properties": {
                    "name": _NULLABLE_STR, "email": _NULLABLE_STR, "phone": _NULLABLE_STR,
                    "service": _NULLABLE_STR, "date": _NULLABLE_STR, "time": _NULLABLE_STR,
                    "note": _NULLABLE_STR, "confirm": {"type": ["boolean", "null"]},
                    "score": {"type": ["string", "null"], "enum": ["hot", "warm", "cold", None]},
                    "score_reason": _NULLABLE_STR,
                    "language": _NULLABLE_STR,
                },
            },
        },
        "required": ["reply", "intent", "fields"],
    },
}


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: float, fast_model: str = ""):
        import anthropic  # imported lazily so the demo runs without the package configured
        self.model, self.fast_model = model, fast_model or model
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def complete(self, system: str, messages: list[dict], structured: bool = True) -> str:
        if not structured:  # plain text from the small model: query rewriting, translation
            resp = self.client.messages.create(model=self.fast_model, max_tokens=300, system=system, messages=messages)
            return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # A tool call returns a valid JSON object (a plain-text JSON answer sometimes came back duplicated or with
        # extra words in live tests). Newer models refuse tool_choice="tool", so we ask for the tool in the prompt
        # and still accept a plain-text answer: parse_model_output() in agent.py handles that case.
        resp = self.client.messages.create(
            model=self.model, max_tokens=700, messages=messages, tools=[REPLY_TOOL],
            system=system + f"\n\nAnswer by calling the {REPLY_TOOL['name']} tool.")
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                return json.dumps(block.input, ensure_ascii=False)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


class OpenAILLM:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: float, fast_model: str = ""):
        import openai
        self.model, self.fast_model = model, fast_model or model
        self.client = openai.OpenAI(api_key=api_key, timeout=timeout)

    def complete(self, system: str, messages: list[dict], structured: bool = True) -> str:
        if not structured:
            resp = self.client.chat.completions.create(
                model=self.fast_model, max_tokens=300,
                messages=[{"role": "system", "content": system}] + messages)
            return (resp.choices[0].message.content or "").strip()
        resp = self.client.chat.completions.create(
            model=self.model, max_tokens=700, response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}] + messages,
        )
        return resp.choices[0].message.content or ""


# ---------------------------------------------------------------- offline mock
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_NAME = re.compile(r"(?:my name is|i am|i'm|name is|name:)\s+([A-Za-z][\w'-]+(?:\s+[A-Za-z][\w'-]+)?)", re.I)
_SERVICES = [
    ("Teeth whitening", ("whiten",)),
    ("Check-up", ("check-up", "checkup", "check up")),
    ("Cleaning", ("cleaning",)),
    ("Filling", ("filling",)),
    ("Orthodontic consultation", ("orthodont", "aligner", "braces")),
]
_NOT_NAMES = {"interested", "looking", "here", "fine", "good", "ok", "okay", "ready", "sorry"}


class MockLLM:
    """Rule-based stand-in so that the demo and the tests run without any API key.
    It is intentionally simple: it extracts fields with regular expressions and answers with the best context chunk."""
    name = "mock"

    def complete(self, system: str, messages: list[dict], structured: bool = True) -> str:
        text = messages[-1]["content"] if messages else ""
        if not structured:  # rewriting / translation: the mock just returns the text unchanged
            return text
        low = text.lower()
        state = self._read_state(system)
        today = self._read_today(system)
        fields = {"name": None, "email": None, "phone": None, "service": None, "date": None, "time": None,
                  "note": None, "language": "en"}

        if m := _EMAIL.search(text):
            fields["email"] = m.group(0)
        if m := _PHONE.search(_DATE.sub("", text)):
            fields["phone"] = m.group(0).strip()
        if m := _DATE.search(text):
            fields["date"] = m.group(1)
        elif "tomorrow" in low:
            fields["date"] = (today + timedelta(days=1)).isoformat()
        if m := _TIME.search(text):
            fields["time"] = f"{int(m.group(1)):02d}:{m.group(2)}"
        if m := _NAME.search(text):
            if m.group(1).split()[0].lower() not in _NOT_NAMES:
                fields["name"] = m.group(1).strip().title()
        for service, keys in _SERVICES:
            if any(k in low for k in keys):
                fields["service"] = service
                break

        fields["score"], fields["score_reason"] = self._score(low, fields, state)
        fields["confirm"] = None
        if state.get("awaiting_confirmation"):
            if re.search(r"\b(yes|yeah|ok|okay|confirm|sure|please do|book it)\b", low):
                fields["confirm"] = True
            elif re.search(r"\b(no|nope|cancel|wrong|change)\b", low):
                fields["confirm"] = False

        merged = {**state, **{k: v for k, v in fields.items() if v}}
        # Moving or cancelling is done by the team: the assistant never starts a new booking for it.
        if not state.get("awaiting_confirmation") and re.search(
                r"\b(reschedule|postpone|(?:cancel|move|change)\s+(?:my|the)\s+(?:\w+\s+)?(?:appointment|booking|visit))\b", low):
            fields["note"] = f"Visitor asks: {text.strip()}"[:250]
            reply = ("I cannot move or cancel appointments myself, our team does that. "
                     + ("I have passed your request to them." if (merged.get("name") and (merged.get("email") or merged.get("phone")))
                        else "I can pass your request to them: please leave your name and an email or phone number."))
            return json.dumps({"reply": reply, "intent": "change", "fields": fields})
        wants_booking = bool(re.search(r"\b(book|appointment|schedule|reserve|visit)\b", low)) or bool(
            state.get("booking_started"))
        if state.get("awaiting_confirmation") or fields["confirm"] is not None:
            wants_booking = True
        gave_contact = bool(fields["email"] or fields["phone"] or fields["name"])
        intent = "book" if wants_booking else ("lead" if gave_contact else "question")

        context = self._read_context(system)
        if intent == "book":
            reply = self._ask_next(merged, today)
        elif intent == "lead":
            reply = "Thank you! I have noted your details."
        elif context:
            reply = self._best_answer(context, text)
        else:
            reply = ("I do not have that information. I can pass your question to our team: "
                     "please leave your name and an email or phone number.")
        return json.dumps({"reply": reply, "intent": intent, "fields": fields})

    @staticmethod
    def _score(low: str, fields: dict, state: dict):
        """Very simple lead scoring for the offline demo; the real model judges from the whole conversation."""
        if re.search(r"\b(pain|hurts?|toothache|urgent|urgently|broken|swollen|emergency|asap)\b", low):
            return "hot", "Reports pain or an urgent problem."
        if fields["date"] or fields["time"] or re.search(r"\b(book|appointment)\b", low):
            return "hot", "Wants to book and named a date or time." if (fields["date"] or fields["time"]) \
                else "Wants to book an appointment."
        if fields["service"] or re.search(r"\b(price|cost|how much)\b", low):
            return "warm", "Asked about a service or its price, no plan yet."
        return None, None

    @staticmethod
    def _ask_next(s: dict, today: date) -> str:
        if not s.get("service"):
            return "Happy to help you book. Which service do you need (check-up, cleaning, whitening, filling, orthodontics)?"
        if not s.get("name"):
            return "May I have your name, please?"
        if not (s.get("email") or s.get("phone")):
            return "What email or phone number can we reach you on?"
        if not s.get("date"):
            return (f"Which day would you like to come? Please give a date, for example "
                    f"{(today + timedelta(days=1)).isoformat()}, or write \"tomorrow\".")
        if not s.get("time"):
            return "What time suits you? We are open 09:00 to 17:00 on weekdays."
        return "Thank you, I am checking the calendar."

    @staticmethod
    def _best_answer(context: str, question: str) -> str:
        """Pick the two context sentences that share the most words with the question."""
        from .rag.retriever import tokenize
        q = set(tokenize(question))
        money = bool(re.search(r"\b(price|prices|cost|costs|much|pay|payment|eur)\b", question.lower()))
        candidates = []
        for block in context.split("\n\n"):
            lines = block.strip().splitlines()
            if not lines:
                continue
            # first line looks like "[faq.md / What are your opening hours?]": the heading helps the score
            heading = set(tokenize(lines[0].split("/", 1)[-1])) if lines[0].startswith("[") else set()
            for line in lines[1:] if lines[0].startswith("[") else lines:
                line = line.strip().lstrip("- ").strip()
                if not line:
                    continue
                for sentence in re.split(r"(?<=[.!?])\s+", line):
                    score = (len(q & set(tokenize(sentence))) + len(q & heading)
                             + (1 if money and "EUR" in sentence else 0))
                    candidates.append((score, len(candidates), sentence))
        best = sorted(candidates, key=lambda c: (-c[0], c[1]))[:2]
        body = " ".join(s for _, _, s in sorted(best, key=lambda c: c[1]))
        return body + " Would you like to book a visit?"

    @staticmethod
    def _read_state(system: str) -> dict:
        m = re.search(r"^STATE: (\{.*\})$", system, re.M)
        try:
            return json.loads(m.group(1)) if m else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _read_today(system: str) -> date:
        m = re.search(r"^TODAY: (\d{4}-\d{2}-\d{2})$", system, re.M)
        return date.fromisoformat(m.group(1)) if m else date.today()

    @staticmethod
    def _read_context(system: str) -> str:
        m = re.search(r"<context>\n(.*?)\n</context>", system, re.S)
        text = m.group(1) if m else ""
        return "" if text.strip().startswith("(nothing relevant") else text


# ---------------------------------------------------------------- fallback chain
class FallbackLLM:
    def __init__(self, providers: list):
        if not providers:
            raise LLMError("No LLM providers configured")
        self.providers = providers
        self.last_used = ""

    def complete(self, system: str, messages: list[dict], structured: bool = True) -> str:
        errors = []
        for provider in self.providers:
            try:
                answer = provider.complete(system, messages, structured=structured)
                if not answer.strip():
                    raise LLMError("empty answer")
                self.last_used = provider.name
                return answer
            except Exception as exc:  # any provider failure -> try the next one
                log.warning("LLM provider %s failed: %s", provider.name, exc)
                errors.append(f"{provider.name}: {exc}")
        raise LLMError("All providers failed: " + "; ".join(errors))


def build_llm(settings: Settings) -> FallbackLLM:
    providers = []
    for name in settings.llm_providers:
        if name == "anthropic" and settings.anthropic_api_key:
            providers.append(AnthropicLLM(settings.anthropic_api_key, settings.anthropic_model, settings.llm_timeout,
                                          settings.anthropic_fast_model))
        elif name == "openai" and settings.openai_api_key:
            providers.append(OpenAILLM(settings.openai_api_key, settings.openai_model, settings.llm_timeout,
                                       settings.openai_fast_model))
        elif name == "mock":
            providers.append(MockLLM())
    if not providers:
        providers.append(MockLLM())
    log.info("LLM chain: %s", " -> ".join(p.name for p in providers))
    return FallbackLLM(providers)
