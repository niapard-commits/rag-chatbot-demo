import pytest

from app.booking import BookingError
from tests.conftest import NOW, SATURDAY, TUESDAY


def book(cal, slot="10:00", day=TUESDAY, who="Anna"):
    return cal.book(session_id=who, name=who, contact="a@b.com", service="Check-up", day=day, slot=slot, now=NOW)


def test_books_a_free_slot(calendar):
    b = book(calendar)
    assert (b.day, b.slot) == (TUESDAY, "10:00")
    assert "10:00" not in calendar.free_slots(TUESDAY, NOW)


def test_double_booking_is_refused_with_alternatives(calendar):
    book(calendar)
    with pytest.raises(BookingError) as err:
        book(calendar, who="Tom")
    assert "already taken" in str(err.value) and "09:00" in str(err.value)


def test_weekend_is_closed(calendar):
    with pytest.raises(BookingError, match="weekend"):
        book(calendar, day=SATURDAY)
    assert calendar.free_slots(SATURDAY, NOW) == []


def test_outside_opening_hours_is_refused(calendar):
    for slot in ("08:00", "17:00", "09:15"):
        with pytest.raises(BookingError):
            book(calendar, slot=slot)


def test_past_time_is_refused(calendar):
    with pytest.raises(BookingError, match="past"):
        calendar.book(session_id="x", name="A", contact="a@b.com", service="Check-up",
                      day="2030-01-07", slot="09:00", now=NOW.replace(hour=12))


def test_bad_date_and_time_give_friendly_errors(calendar):
    with pytest.raises(BookingError, match="date"):
        book(calendar, day="tomorrow")
    with pytest.raises(BookingError, match="time"):
        book(calendar, slot="noon")
