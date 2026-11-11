import json

import pytest

from app.agent import FALLBACK_REPLY, ChatAgent, clean_fields, parse_model_output
from app.llm import FallbackLLM
from app.rag.retriever import BM25Retriever
from tests.conftest import NOW, TUESDAY


def say(agent, session, text):
    return agent.handle(session, text, now=NOW)


def test_answers_from_the_knowledge_base(agent):
    r = say(agent, "s1", "What are your opening hours?")
    assert "09:00" in r["reply"] and "17:00" in r["reply"]
    assert any("opening hours" in s for s in r["sources"])


def test_unknown_question_is_not_invented(agent):
    r = say(agent, "s1", "Do you sell used cars?")
    assert "do not have that information" in r["reply"]


def test_full_booking_flow_saves_lead_and_appointment(agent, crm, calendar):
    say(agent, "s1", "I want to book a teeth whitening")
    say(agent, "s1", "My name is Anna Novak")
    say(agent, "s1", "anna@example.com")
    r = say(agent, "s1", f"{TUESDAY} at 10:00")
    assert r["booking"] is None and "Please check" in r["reply"] and "Tuesday" in r["reply"]   # asks first
    r = say(agent, "s1", "yes")
    assert r["booking"] == {"id": 1, "service": "Teeth whitening", "day": TUESDAY, "time": "10:00"}
    assert "booked" in r["reply"]
    leads = crm.list_leads()
    assert len(leads) == 1 and leads[0]["email"] == "anna@example.com" and leads[0]["service"] == "Teeth whitening"
    assert "10:00" in calendar.taken(TUESDAY)


def test_taken_slot_asks_for_another_time(agent):
    say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {TUESDAY} 10:00")
    say(agent, "s1", "yes")
    say(agent, "s2", f"Book a cleaning, I'm Tom Lee, tom@example.com, {TUESDAY} 10:00")
    r = say(agent, "s2", "yes")
    assert r["booking"] is None and "already taken" in r["reply"]
    assert "date" not in r["state"] and "time" not in r["state"]     # asks again for date and time
    say(agent, "s2", f"{TUESDAY} 11:00")
    r = say(agent, "s2", "yes")
    assert r["booking"]["time"] == "11:00"


def test_no_booking_without_explicit_yes(agent, calendar):
    r = say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {TUESDAY} 10:00")
    assert r["booking"] is None and calendar.taken(TUESDAY) == set()
    r = say(agent, "s1", "hmm, what does it cost?")              # not a yes: still nothing booked
    assert r["booking"] is None and calendar.taken(TUESDAY) == set()


def test_declining_the_summary_books_nothing(agent, calendar):
    say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {TUESDAY} 10:00")
    r = say(agent, "s1", "no, wrong day")
    assert r["booking"] is None and "not booked" in r["reply"] and calendar.taken(TUESDAY) == set()


def test_changed_details_need_a_new_confirmation(agent, calendar):
    say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {TUESDAY} 10:00")
    r = say(agent, "s1", "better 15:00")                         # changes the time instead of confirming
    assert r["booking"] is None and "15:00" in r["reply"]
    r = say(agent, "s1", "yes")
    assert r["booking"]["time"] == "15:00" and calendar.taken(TUESDAY) == {"15:00"}


class ScriptedLLM:
    """Returns prepared answers and remembers what it was asked."""
    name = "scripted"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, messages, structured=True):
        self.calls.append((system, messages, structured))
        return self.answers.pop(0)


def test_model_cannot_confirm_in_the_same_turn(settings, calendar, crm):
    """Even if the model claims confirm=true on the first message, nothing is booked before the summary."""
    answer = json.dumps({"reply": "Booked!", "intent": "book", "fields": {
        "name": "Eve", "email": "eve@example.com", "service": "Check-up", "date": TUESDAY, "time": "10:00",
        "confirm": True}})
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([ScriptedLLM(answer)]), crm, calendar)
    r = a.handle("s1", "book", now=NOW)
    assert r["booking"] is None and "Please check" in r["reply"] and calendar.taken(TUESDAY) == set()


def test_unknown_service_is_dropped(agent):
    a = say(agent, "s1", "I want to book dental implants")
    assert "service" not in a["state"]


def test_context_from_earlier_questions_stays_in_the_prompt(settings, calendar, crm):
    ok = json.dumps({"reply": "ok", "intent": "question", "fields": {}})
    llm = ScriptedLLM(ok, ok)
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    a.handle("s1", "What are your opening hours?", now=NOW)
    a.handle("s1", "Do you treat children?", now=NOW)
    second_prompt = llm.calls[1][0]
    assert "Children from 3 years old" in second_prompt and "Monday to Friday" in second_prompt


def test_question_for_the_team_is_saved_with_the_lead(settings, calendar, crm):
    answers = [json.dumps({"reply": "I can ask the team. Your name and phone?", "intent": "question",
                           "fields": {"note": "wants to know about implants"}}),
               json.dumps({"reply": "Thanks!", "intent": "lead",
                           "fields": {"name": "Lucia Kral", "phone": "0905 333 444"}})]
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([ScriptedLLM(*answers)]), crm, calendar)
    a.handle("s1", "Do you do implants?", now=NOW)
    a.handle("s1", "Lucia Kral, 0905 333 444", now=NOW)
    lead = crm.list_leads()[0]
    assert lead["name"] == "Lucia Kral" and lead["note"] == "wants to know about implants"


def test_lead_without_booking_is_saved(agent, crm):
    say(agent, "s9", "My name is Eva Horvath")
    say(agent, "s9", "call me on +421 900 123 456")
    leads = crm.list_leads()
    assert leads[0]["name"] == "Eva Horvath" and leads[0]["phone"] == "+421 900 123 456"


def test_sessions_do_not_share_state(agent):
    say(agent, "a", "I want to book a cleaning")
    assert "service" not in say(agent, "b", "hello")["state"]


def test_empty_message_is_rejected(agent):
    with pytest.raises(ValueError):
        agent.handle("s1", "   ")


def test_all_llms_down_gives_a_polite_fallback(settings, calendar, crm):
    class Down:
        name = "down"

        def complete(self, system, messages, structured=True):
            raise ConnectionError("no network")

    broken = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([Down()]), crm, calendar)
    r = broken.handle("s1", "hello")
    assert r["reply"] == FALLBACK_REPLY and r["provider"] is None


# ------------------------------------------------------------ parsing of real-model output
def test_parse_json_inside_code_fence():
    raw = '```json\n' + json.dumps({"reply": "Hi", "intent": "book", "fields": {"name": "Anna"}}) + '\n```'
    p = parse_model_output(raw)
    assert p["reply"] == "Hi" and p["intent"] == "book" and p["fields"] == {"name": "Anna"}


def test_parse_plain_text_when_model_ignores_format():
    p = parse_model_output("Sure, we are open 9 to 5.")
    assert p["reply"] == "Sure, we are open 9 to 5." and p["fields"] == {}


def test_parse_survives_duplicated_json_and_chatter():
    one = json.dumps({"reply": "Real answer", "intent": "question", "fields": {}})
    raw = f"Real answer\n\n{one}\n\nCorrection: here is the JSON only.\n\n{one}"
    assert parse_model_output(raw)["reply"] == "Real answer"
    assert "{" not in parse_model_output("Plain text first {broken json")["reply"]


def test_invalid_intent_becomes_question():
    assert parse_model_output('{"reply": "x", "intent": "hack"}')["intent"] == "question"


def test_clean_fields_drops_invalid_values():
    out = clean_fields({"name": "A", "email": "not-an-email", "phone": "12", "date": "31/01/2030",
                        "time": "25:99", "service": "x" * 100})
    assert out == {}
    ok = clean_fields({"name": "Anna", "email": "a@b.co", "phone": "+421 900 123 456",
                       "date": "2030-01-08", "time": "9:30", "service": "Check-up"})
    assert ok["time"] == "09:30" and ok["date"] == "2030-01-08" and len(ok) == 6


def test_clean_fields_checks_service_against_the_list():
    services = ["Check-up", "Teeth whitening"]
    assert clean_fields({"service": "teeth WHITENING"}, services) == {"service": "Teeth whitening"}
    assert clean_fields({"service": "Dental implants"}, services) == {}


def test_other_language_question_is_searched_with_an_english_query(settings, calendar, crm):
    settings.query_rewrite = True
    ok = json.dumps({"reply": "ok", "intent": "question", "fields": {"language": "uk"}})
    llm = ScriptedLLM("opening hours", ok)                  # 1st call: rewrite, 2nd call: the answer
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    r = a.handle("s1", "Коли ви працюєте?", now=NOW)
    assert llm.calls[0][2] is False                         # the rewrite call is plain text
    assert any("opening hours" in src for src in r["sources"])


def test_rewrite_failure_falls_back_to_the_original_message(settings, calendar, crm):
    from app.llm import LLMError

    class RewriteBroken(ScriptedLLM):
        def complete(self, system, messages, structured=True):
            if not structured:
                raise LLMError("rewrite model down")
            return super().complete(system, messages, structured)

    settings.query_rewrite = True
    ok = json.dumps({"reply": "ok", "intent": "question", "fields": {}})
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([RewriteBroken(ok)]), crm, calendar)
    assert a.handle("s1", "What are your opening hours?", now=NOW)["reply"] == "ok"


def test_booking_summary_is_translated_for_the_visitor(settings, calendar, crm):
    answer = json.dumps({"reply": "x", "intent": "book", "fields": {
        "name": "Olena", "email": "o@example.com", "service": "Check-up", "date": TUESDAY, "time": "10:00",
        "language": "uk"}})
    llm = ScriptedLLM(answer, "Перевірте: Check-up, вівторок 2030-01-08, 10:00. Записати? (так / ні)")
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    r = a.handle("s1", "...", now=NOW)
    assert r["reply"].startswith("Перевірте") and llm.calls[1][2] is False


def test_plain_english_question_with_a_hit_skips_the_rewrite_call(settings, calendar, crm):
    settings.query_rewrite = True
    ok = json.dumps({"reply": "ok", "intent": "question", "fields": {}})
    llm = ScriptedLLM(ok)                                   # only one call is expected
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    a.handle("s1", "What are your opening hours today?", now=NOW)
    assert len(llm.calls) == 1 and llm.calls[0][2] is True


def test_early_facts_stay_in_context_after_several_questions(settings, calendar, crm):
    ok = json.dumps({"reply": "ok", "intent": "question", "fields": {}})
    llm = ScriptedLLM(*[ok] * 4)
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    for q in ["What are your opening hours?", "How much is teeth whitening?", "Where is the clinic?",
              "How can I cancel my appointment?"]:
        a.handle("s1", q, now=NOW)
    assert "Monday to Friday" in llm.calls[3][0]            # the first answer's source is still there
