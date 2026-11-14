from app.agent import clean_translation


def test_translation_thinking_aloud_is_removed():
    src = "Please check: Cleaning on Wednesday 2026-11-18 at 12:00 for Anna. Shall I book it? (yes / no)"
    bad = ("Будь ласка, перевірте: Пломбування у середу 2026-11-18 о 12:00 для Anna. Записати?? (так / ні)\n\n"
           "Wait, correcting the double question mark:\n\n"
           "Будь ласка, перевірте: Пломбування у середу 2026-11-18 о 12:00 для Anna. Записати? (так / ні)")
    out = clean_translation(src, bad)
    assert "Wait" not in out and out.count("Будь ласка") == 1 and "??" not in out


def test_clean_translation_keeps_paragraphs_of_the_source():
    assert clean_translation("A\n\nB", "а\n\nб") == "а\n\nб"
    assert clean_translation("A", "  а  ") == "а"


def test_fallback_reply_follows_the_visitors_language():
    from app.agent import FALLBACK_REPLY, fallback_reply
    assert fallback_reply("uk").startswith("Вибачте")
    assert fallback_reply("en") == FALLBACK_REPLY and fallback_reply(None) == FALLBACK_REPLY


def test_unusable_model_answer_is_marked():
    from app.agent import parse_model_output
    assert parse_model_output('{"intent": "question"}').get("unusable") is True
    assert not parse_model_output('{"reply": "Hi", "intent": "question"}').get("unusable")
