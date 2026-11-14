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
