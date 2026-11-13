import json

import pytest

from app.agent import FALLBACK_REPLY, ChatAgent, clean_fields, parse_model_output, short_answer
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
    # The second visitor is told BEFORE being asked to confirm (no "Please check" for an impossible slot).
    r = say(agent, "s2", f"Book a cleaning, I'm Tom Lee, tom@example.com, {TUESDAY} 10:00")
    assert r["booking"] is None and "already taken" in r["reply"] and "Please check" not in r["reply"]
    assert "date" not in r["state"] and "time" not in r["state"]     # asks again for date and time
    r = say(agent, "s2", f"{TUESDAY} 11:00")
    assert "Please check" in r["reply"]
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


# ------------------------------------------------------------ bugs found in live tests (12 Nov 2026)
LEAK = ('<invoke name="reply_to_visitor">\n<parameter name="reply">I do not have that information. '
        'What time on Monday suits you, between 09:00 and 17:00?</parameter>\n'
        '<parameter name="intent">book</parameter>\n<parameter name="fields">')


def test_tool_call_markup_never_reaches_the_visitor():
    p = parse_model_output(LEAK)
    assert p["reply"] == "I do not have that information. What time on Monday suits you, between 09:00 and 17:00?"
    assert p["intent"] == "book" and p["fields"] == {} and p["repaired"] is True
    assert "<" not in p["reply"]


def test_markup_with_valid_fields_keeps_the_fields():
    raw = ('<invoke name="reply_to_visitor"><parameter name="reply">Hi Anna</parameter>'
           '<parameter name="intent">lead</parameter><parameter name="fields">{"name": "Anna"}</parameter></invoke>')
    p = parse_model_output(raw)
    assert p["reply"] == "Hi Anna" and p["intent"] == "lead" and p["fields"] == {"name": "Anna"}


def test_markup_inside_a_json_reply_is_removed():
    raw = json.dumps({"reply": "Sure.<parameter name=\"x\">", "intent": "question", "fields": {}})
    assert parse_model_output(raw)["reply"] == "Sure."


def test_cut_off_json_still_gives_the_readable_reply():
    p = parse_model_output('{"reply": "We are open Monday to Friday, 09:00 to 17:00. Anything el')
    assert p["reply"].startswith("We are open Monday to Friday") and p["repaired"] is True


def test_unreadable_output_gives_the_polite_fallback():
    assert parse_model_output('{"intent": "book", "fie')["reply"] == FALLBACK_REPLY


def test_lost_booking_fields_are_asked_for_again_once(settings, calendar, crm):
    good = json.dumps({"reply": "Thank you.", "intent": "book", "fields": {
        "name": "Eve", "email": "eve@example.com", "service": "Check-up", "date": TUESDAY, "time": "10:00"}})
    llm = ScriptedLLM(LEAK, good)
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([llm]), crm, calendar)
    r = a.handle("s1", "book a check-up", now=NOW)
    assert len(llm.calls) == 2 and "Please check" in r["reply"] and "<" not in r["reply"]


@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes!", True), ("yes please", True), ("ok", True), ("book it", True),
    ("так", True), ("Так, підтверджую", True), ("ага", True), ("да", True), ("ano", True), ("tak", True),
    ("no", False), ("ні", False), ("нет", False), ("nie", False), ("no thanks", False),
    ("yes 15:00", None), ("ні, давайте інший час", None), ("what does it cost?", None), ("", None),
    ("yes but can you do it on a different day please", None),
])
def test_short_answers_are_read_in_any_language(text, expected):
    assert short_answer(text) is expected


def test_ukrainian_yes_confirms_an_english_summary(agent, calendar):
    r = say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {TUESDAY} 10:00")
    assert "Please check" in r["reply"]
    r = say(agent, "s1", "так")
    assert r["booking"]["time"] == "10:00" and calendar.taken(TUESDAY) == {"10:00"}


def test_a_question_while_waiting_gets_an_answer_and_one_reminder(settings, calendar, crm):
    book = json.dumps({"reply": "Thank you.", "intent": "book", "fields": {
        "name": "Anna Novak", "email": "anna@example.com", "service": "Cleaning", "date": TUESDAY, "time": "10:00"}})
    side = json.dumps({"reply": "We are open Monday to Friday, 09:00 to 17:00.", "intent": "question", "fields": {}})
    ok = json.dumps({"reply": "Great.", "intent": "book", "fields": {}})
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([ScriptedLLM(book, side, ok)]),
                  crm, calendar)
    assert "Please check" in a.handle("s1", "book a cleaning", now=NOW)["reply"]
    r = a.handle("s1", "What are your opening hours?", now=NOW)
    assert r["reply"].startswith("We are open Monday to Friday")          # the question is answered
    assert r["reply"].count("Shall I book it?") == 1 and "not confirmed yet" in r["reply"]
    assert r["booking"] is None and calendar.taken(TUESDAY) == set()
    r = a.handle("s1", "yes", now=NOW)                                     # the booking is still waiting
    assert r["booking"]["time"] == "10:00"


@pytest.mark.parametrize("when,message", [
    ("2030-01-08 12:34", "every 30 minutes"),
    ("2030-01-12 10:00", "weekend"),
    ("2030-12-31 10:00", "180 days"),
    ("2030-01-04 10:00", "past"),
])
def test_impossible_times_are_refused_before_asking_to_confirm(agent, calendar, when, message):
    r = say(agent, "s1", f"Book a cleaning, I'm Anna Novak, anna@example.com, {when}")
    assert message in r["reply"] and "Please check" not in r["reply"] and "Shall I book" not in r["reply"]
    assert "date" not in r["state"] and "time" not in r["state"] and "awaiting_confirmation" not in r["state"]
    assert calendar.taken("2030-01-08") == set() and calendar.taken("2030-01-04") == set()


@pytest.mark.parametrize("when,message", [
    ("2030-01-08 12:34", "every 30 minutes"),
    ("2030-01-12 10:00", "weekend"),
])
def test_impossible_time_is_refused_before_asking_for_name_and_contact(agent, when, message):
    # Found live: "tomorrow at 12:34" was accepted and the bot asked for a name first.
    r = say(agent, "s1", f"I want to book a cleaning on {when.split()[0]} at {when.split()[1]}")
    assert message in r["reply"] and "Which other day and time" in r["reply"]
    assert "date" not in r["state"] and "time" not in r["state"]


def test_weekend_day_is_refused_even_before_a_time_is_given(agent):
    # Found live: "book a cleaning tomorrow" (a Saturday) got "what time would you like?"
    r = say(agent, "s1", "I want to book a cleaning on 2030-01-12")
    assert "weekend" in r["reply"] and "date" not in r["state"]


def test_moving_an_appointment_never_starts_a_new_booking(agent, calendar, crm):
    r = say(agent, "s1", "Please reschedule my appointment")
    assert "our team" in r["reply"] and r["booking"] is None and "booking_started" not in r["state"]
    r = say(agent, "s1", f"I'm Anna Novak, anna@example.com, maybe {TUESDAY} 10:00")
    assert calendar.taken(TUESDAY) == set()
    assert crm.list_leads()[0]["note"].startswith("Visitor asks: Please reschedule")


def test_change_intent_from_the_model_ignores_booking_details(settings, calendar, crm):
    answer = json.dumps({"reply": "Our team does that. Shall I pass it on?", "intent": "change", "fields": {
        "name": "Eve", "email": "eve@example.com", "service": "Filling", "date": TUESDAY, "time": "10:00",
        "note": "wants to move the filling to Thursday"}})
    a = ChatAgent(settings, BM25Retriever(settings.knowledge_dir), FallbackLLM([ScriptedLLM(answer)]), crm, calendar)
    r = a.handle("s1", "can you move my filling to Thursday?", now=NOW)
    assert r["booking"] is None and "Please check" not in r["reply"] and calendar.taken(TUESDAY) == set()
    assert crm.list_leads()[0]["note"] == "wants to move the filling to Thursday"


def test_page_stops_browser_auto_translation():
    html = open("static/index.html", encoding="utf-8").read()
    assert 'translate="no"' in html and 'name="google" content="notranslate"' in html


def test_page_starts_a_new_conversation_on_every_load():
    # The chat window is empty after a reload, so the bot must not remember the earlier visit.
    html = open("static/index.html", encoding="utf-8").read()
    assert "localStorage.getItem" not in html and "localStorage.setItem" not in html
