"""The chat agent: retrieve knowledge -> ask the LLM -> validate what it extracted -> save lead / book -> reply.

The LLM only talks and extracts data. Everything with consequences (saving a lead, booking a slot) is done by
plain code after validation, so a wrong or invented model answer can never create a booking by itself.
"""
import json
import logging
import re
import unicodedata
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
EMAIL_RE = re.compile(r"^\w[\w.+-]*@[\w-]+(?:\.[\w-]+)+$")
PHONE_RE = re.compile(r"^[+(]?[\d\s().-]{6,24}$")
FALLBACK_REPLY = ("Sorry, I am having technical trouble right now. Please try again in a minute "
                  "or leave your name and phone number and our team will call you back.")


INTENTS = ("question", "lead", "book", "change")
# Tool-call markup that a model sometimes writes into its text answer instead of making a real tool call
# (seen in a live test: the visitor saw <invoke name="reply_to_visitor"><parameter name="reply">...).
_TAG_RE = re.compile(r"</?\s*(?:antml:)?(?:function_calls|invoke|parameter|tool_use|tool_call)\b[^>]*>?", re.I)
_PARAM_RE = re.compile(
    r'<\s*(?:antml:)?parameter\s+name="(\w+)"\s*>(.*?)'
    r'(?:<\s*/\s*(?:antml:)?parameter\s*>|(?=<\s*(?:antml:)?parameter\s+name=)|\Z)', re.S | re.I)


def strip_markup(text: str) -> str:
    return _TAG_RE.sub("", text).strip()


def _intent(value) -> str:
    return value.strip().lower() if isinstance(value, str) and value.strip().lower() in INTENTS else "question"


def _from_markup(text: str) -> dict | None:
    """Recover the answer from tool-call markup written as text. Fields are kept only if they are valid JSON."""
    params = {m.group(1).lower(): m.group(2).strip() for m in _PARAM_RE.finditer(text)}
    reply = strip_markup(params.get("reply", ""))
    if not reply:
        return None
    fields = {}
    if params.get("fields"):
        try:
            data = json.loads(params["fields"])
            fields = data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    return {"reply": reply, "intent": _intent(params.get("intent")), "fields": fields, "repaired": True}


def _salvage_reply(text: str) -> dict | None:
    """The JSON was cut off or is broken, but the reply text is readable: show that, extract nothing else."""
    m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not m:
        return None
    raw = m.group(1).rstrip("\\")
    try:
        reply = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        reply = raw
    reply = strip_markup(reply)
    if not reply:
        return None
    intent = re.search(r'"intent"\s*:\s*"(\w+)"', text)
    return {"reply": reply, "intent": _intent(intent.group(1) if intent else None), "fields": {}, "repaired": True}


def parse_model_output(raw: str) -> dict:
    """Return {"reply", "intent", "fields"} even if the model wrapped the JSON in text or code fences,
    wrote tool-call markup as text, or its JSON was cut off. "repaired" is added when the answer had to be rescued."""
    text = raw.strip()
    if re.search(r"<\s*(?:antml:)?parameter\s+name=", text, re.I):
        recovered = _from_markup(text)
        if recovered:
            return recovered
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:  # take the first valid JSON object that has a reply, even if there is text around it
        try:
            data, _ = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            pos = text.find("{", pos + 1)
            continue
        if isinstance(data, dict) and isinstance(data.get("reply"), str) and strip_markup(data["reply"]):
            fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
            return {"reply": strip_markup(data["reply"]), "intent": _intent(data.get("intent")), "fields": fields}
        pos = text.find("{", pos + 1)
    # The model ignored the format: use the text before any JSON as the reply and extract nothing.
    plain = strip_markup(re.sub(r"```(?:json)?", "", text.split("{", 1)[0]))
    if not plain and (salvaged := _salvage_reply(text)):
        return salvaged
    return {"reply": plain or FALLBACK_REPLY, "intent": "question", "fields": {}}


# A short answer to "Shall I book it? (yes / no)" in the languages our visitors write. The model also reports
# "confirm", but it missed a plain Ukrainian "так" in an English conversation, so short answers are read by code.
_YES = {"yes", "y", "yeah", "yea", "yep", "yup", "sure", "ok", "okay", "confirm", "confirmed", "correct", "right",
        "да", "так", "ага", "угу", "авжеж", "звісно", "звичайно", "добре", "гаразд", "окей", "ок", "підтверджую",
        "підтверджуємо", "підтвердити", "підтвердьте", "підходить", "давайте", "давай", "бронюйте", "бронюй",
        "забронюйте", "записуйте", "ano", "áno", "jo", "jasne", "tak", "si", "sí", "oui", "ja", "claro", "aha"}
_NO = {"no", "n", "nope", "nah", "cancel", "wrong", "incorrect", "ні", "нет", "не", "нє", "ніт", "nie", "ne", "nein",
       "non", "stop"}
_FILLER = {"please", "thanks", "thank", "you", "so", "then", "well", "будь", "ласка", "прошу", "дякую", "ну", "prosim",
           "prosím", "ďakujem", "dakujem"}
_YES_PHRASES = {"book it", "go ahead", "do it", "please do", "sounds good", "looks good", "that is right",
                "that is correct", "thats right", "thats correct"}


def short_answer(message: str) -> bool | None:
    """True / False for a short clear yes / no, None for anything else (a question, new details, a longer text)."""
    words = re.findall(r"[^\W_]+", message.lower().replace("'", "").replace("\u2019", ""))
    if not words or len(words) > 5:
        return None
    if " ".join(words) in _YES_PHRASES:
        return True
    if any(w not in _YES | _NO | _FILLER for w in words):
        return None
    yes, no = any(w in _YES for w in words), any(w in _NO for w in words)
    return True if yes and not no else False if no and not yes else None


def clean_text(value: str, limit: int) -> str:
    """Free text from a visitor ends up in a CRM, a spreadsheet or an e-mail. Remove what must never get there:
    control and invisible formatting characters (zero-width, right-to-left override, null bytes), and a leading
    = + - @ which a spreadsheet would run as a formula (CSV / Google Sheets formula injection)."""
    kept = "".join(" " if ch in "\n\r\t" else ch for ch in value if ch in "\n\r\t" or unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", kept).strip().lstrip("=+-@ ")[:limit].strip()


def clean_fields(fields: dict, services: list[str] | None = None) -> dict:
    """Keep only values that look valid. Anything else is dropped, so the bot asks again.
    If `services` is given, "service" must be one of them (case-insensitive) and is stored with the canonical spelling."""
    out = {}
    name = fields.get("name")
    if isinstance(name, str) and 1 < len(clean_text(name, 200)) <= 60 and any(c.isalpha() for c in name):
        out["name"] = clean_text(name, 60)
    email = fields.get("email")
    if isinstance(email, str) and EMAIL_RE.match(email.strip()):
        out["email"] = email.strip()
    phone = fields.get("phone")
    if isinstance(phone, str) and PHONE_RE.match(phone.strip()) and 7 <= len(re.sub(r"\D", "", phone)) <= 15:
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
    if isinstance(note, str) and clean_text(note, 300):
        out["note"] = clean_text(note, 300)
    score = fields.get("score")
    if isinstance(score, str) and score.strip().lower() in LEAD_SCORES:
        out["score"] = score.strip().lower()
        reason = fields.get("score_reason")
        if isinstance(reason, str) and clean_text(reason, 200):
            out["score_reason"] = clean_text(reason, 200)
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
            if parsed.get("repaired"):
                log.warning("The model did not answer in the expected format; the reply was recovered from its text")
                if parsed["intent"] == "book" and not parsed["fields"]:
                    # Booking details were lost with the broken format: ask once more instead of losing them.
                    again = parse_model_output(self.llm.complete(system, messages))
                    if not again.get("repaired") or again["fields"]:
                        parsed = again
        except LLMError as exc:
            log.error("All LLM providers failed: %s", exc)
            return {"reply": FALLBACK_REPLY, "state": self._public_state(state), "sources": [],
                    "provider": None, "booking": None}

        confirm = parsed["fields"].get("confirm") if isinstance(parsed["fields"], dict) else None
        if state.get("_pending") and (direct := short_answer(message)) is not None:
            confirm = direct                      # a plain "так" / "yes" / "no" is read by code, in any language
        state.update(clean_fields(parsed["fields"], self.settings.services))
        if state.get("_booked_reason"):  # once a visitor has booked, the model cannot lower the score again
            state["score"], state["score_reason"] = "hot", state["_booked_reason"]
        if parsed["intent"] == "book":
            state["booking_started"] = True

        reply, booking = parsed["reply"], None
        self._maybe_save_lead(session_id, state)
        # "change" = the visitor wants to move or cancel an appointment. The assistant cannot do that: the team does.
        booking_reply = None if parsed["intent"] == "change" else self._maybe_book(session_id, state, now, confirm)
        if booking_reply:
            text, booking, replaces_reply = booking_reply
            text = self._localize(text, state.get("language"))
            reply = text if replaces_reply else f"{reply}\n\n{text}"

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
        """Returns None or (text, booking_info, replaces_the_model_reply)."""
        if not state.get("booking_started"):
            return None
        # As soon as the day and the time are both known, check them. Waiting until the name and the contact are
        # collected too meant the visitor was asked for personal details first and only then heard "we are closed".
        if state.get("date") and state.get("time"):
            try:
                self.calendar.check(state["date"], state["time"], now)
            except BookingError as exc:
                self._forget_pending(state, with_slot=True)
                return str(exc) + " Which other day and time would you prefer?", None, True
        contact = state.get("email") or state.get("phone")
        if not (state.get("service") and state.get("date") and state.get("time") and state.get("name") and contact):
            return None
        # Never book straight away: the model may have misread a relative date ("next Thursday").
        # The code shows its own summary and books only after an explicit yes about exactly these details.
        signature = (state["service"], state["date"], state["time"], state["name"], contact)
        pending = state.get("_pending") == signature
        if pending and confirm is False:
            self._forget_pending(state, with_slot=True)
            return "No problem, I have not booked anything. Which day and time would suit you instead?", None, True
        if not pending:
            # Check the day and time BEFORE asking for a confirmation (weekend, 12:34, past, too far ahead, taken).
            try:
                self.calendar.check(state["date"], state["time"], now)
            except BookingError as exc:
                self._forget_pending(state, with_slot=True)
                return str(exc) + " Which other day and time would you prefer?", None, True
            state["_pending"] = signature
            weekday = date.fromisoformat(state["date"]).strftime("%A")
            summary = f"{state['service']} on {weekday} {state['date']} at {state['time']} for {state['name']} ({contact})"
            state["awaiting_confirmation"] = summary
            return f"Please check: {summary}. Shall I book it? (yes / no)", None, True
        if confirm is not True:
            # Same details, no clear answer (the visitor asked something else): the model's answer stays, the
            # code only reminds that the booking is still waiting. The confirmation question is not repeated.
            return f"Your booking is not confirmed yet: {state['awaiting_confirmation']}. Shall I book it? (yes / no)", \
                None, False
        self._forget_pending(state)
        try:
            b = self.calendar.book(session_id=session_id, name=state["name"], contact=contact,
                                   service=state["service"], day=state["date"], slot=state["time"], now=now)
        except BookingError as exc:
            # Forget the rejected date/time so the visitor can choose another one.
            state.pop("date", None)
            state.pop("time", None)
            return str(exc) + " Which other day and time would you prefer?", None, True
        self.crm.notify_booking(session_id, state["name"], contact, b.service, b.day, b.slot)
        # A confirmed booking is the strongest buying signal there is: the code sets the score, not the model.
        state["_booked_reason"] = f"Booked {b.service} on {b.day} at {b.slot}."
        state["score"], state["score_reason"] = "hot", state["_booked_reason"]
        self._maybe_save_lead(session_id, state)
        for key in ("service", "date", "time", "booking_started"):
            state.pop(key, None)
        info = {"id": b.id, "service": b.service, "day": b.day, "time": b.slot}
        return (f"Your appointment is booked: {b.service} on {b.day} at {b.slot}. "
                f"We will contact you at {contact} if anything changes. Anything else I can help with?"), info, True

    @staticmethod
    def _forget_pending(state: dict, with_slot: bool = False) -> None:
        state.pop("_pending", None)
        state.pop("awaiting_confirmation", None)
        if with_slot:
            state.pop("date", None)
            state.pop("time", None)

    @staticmethod
    def _public_state(state: dict) -> dict:
        return {k: v for k, v in state.items() if not k.startswith("_")}
