import datetime as dt

from app.router.date_phrase import parse_date_phrase

# Wednesday, 2026-08-26 — matches the real "today" this conversation is
# pinned to, but hardcoded so these tests never depend on the actual clock.
TODAY = dt.date(2026, 8, 26)


def test_today():
    assert parse_date_phrase("today", TODAY) == TODAY


def test_tomorrow():
    assert parse_date_phrase("tomorrow", TODAY) == dt.date(2026, 8, 27)


def test_in_n_days():
    assert parse_date_phrase("in 3 days", TODAY) == dt.date(2026, 8, 29)


def test_in_zero_days_is_today():
    assert parse_date_phrase("in 0 days", TODAY) == TODAY


def test_in_n_weeks():
    assert parse_date_phrase("in 2 weeks", TODAY) == dt.date(2026, 9, 9)


def test_next_week():
    assert parse_date_phrase("next week", TODAY) == dt.date(2026, 9, 2)


def test_end_of_month():
    assert parse_date_phrase("end of month", TODAY) == dt.date(2026, 8, 31)


def test_end_of_month_variants():
    assert parse_date_phrase("end of the month", TODAY) == dt.date(2026, 8, 31)
    assert parse_date_phrase("EOM", TODAY) == dt.date(2026, 8, 31)


def test_bare_weekday_matching_today_resolves_to_today():
    # 2026-08-26 is a Wednesday
    assert parse_date_phrase("wednesday", TODAY) == TODAY


def test_bare_weekday_later_this_week():
    assert parse_date_phrase("friday", TODAY) == dt.date(2026, 8, 28)


def test_bare_weekday_earlier_in_week_rolls_to_next_week():
    # Monday already passed this week -> next Monday, not this one
    assert parse_date_phrase("monday", TODAY) == dt.date(2026, 8, 31)


def test_weekday_abbreviation():
    assert parse_date_phrase("fri", TODAY) == dt.date(2026, 8, 28)


def test_next_weekday_said_on_that_day_skips_a_full_week():
    assert parse_date_phrase("next wednesday", TODAY) == dt.date(2026, 9, 2)


def test_next_weekday_said_earlier_in_week_is_still_this_weeks_occurrence():
    # documented, chosen behavior: "next <weekday>" = nearest upcoming
    # occurrence, excluding today — same as bare weekday unless today IS
    # that weekday. Not every English speaker would read it this way, but
    # it's one consistent rule, always documented.
    assert parse_date_phrase("next friday", TODAY) == dt.date(2026, 8, 28)


def test_numeric_dd_mm_no_year_future_this_year():
    assert parse_date_phrase("30-08", TODAY) == dt.date(2026, 8, 30)


def test_numeric_dd_mm_no_year_already_passed_rolls_to_next_year():
    assert parse_date_phrase("05-01", TODAY) == dt.date(2027, 1, 5)


def test_numeric_dd_mm_yyyy_slashes():
    assert parse_date_phrase("30/08/2026", TODAY) == dt.date(2026, 8, 30)


def test_numeric_dd_mm_two_digit_year():
    assert parse_date_phrase("30-08-26", TODAY) == dt.date(2026, 8, 30)


def test_numeric_is_day_first_not_month_first():
    # Indian convention, matches the rest of the app (IST/INR) — 3/4 means
    # 3rd April, not March 4th.
    assert parse_date_phrase("3/4", TODAY) == dt.date(2027, 4, 3)


def test_day_month_name_hyphenated():
    assert parse_date_phrase("30-Aug", TODAY) == dt.date(2026, 8, 30)


def test_day_month_name_full():
    assert parse_date_phrase("30 August", TODAY) == dt.date(2026, 8, 30)


def test_day_month_name_with_ordinal_suffix():
    assert parse_date_phrase("30th Aug", TODAY) == dt.date(2026, 8, 30)


def test_day_month_name_with_year():
    assert parse_date_phrase("15 Dec 2026", TODAY) == dt.date(2026, 12, 15)


def test_month_day_format():
    assert parse_date_phrase("Aug 30", TODAY) == dt.date(2026, 8, 30)


def test_month_day_with_comma_and_year():
    assert parse_date_phrase("Dec 15, 2026", TODAY) == dt.date(2026, 12, 15)


def test_case_insensitive_and_whitespace_tolerant():
    assert parse_date_phrase("  TOMORROW  ", TODAY) == dt.date(2026, 8, 27)
    assert parse_date_phrase("FRIDAY", TODAY) == dt.date(2026, 8, 28)


def test_trailing_punctuation_stripped():
    assert parse_date_phrase("tomorrow!", TODAY) == dt.date(2026, 8, 27)
    assert parse_date_phrase("tomorrow.", TODAY) == dt.date(2026, 8, 27)


def test_invalid_calendar_date_rejected():
    assert parse_date_phrase("31-02", TODAY) is None  # Feb 31 doesn't exist


def test_invalid_month_number_rejected():
    assert parse_date_phrase("15-13", TODAY) is None  # month 13


def test_unrecognized_phrase_rejected():
    assert parse_date_phrase("whenever", TODAY) is None
    assert parse_date_phrase("asap", TODAY) is None
    assert parse_date_phrase("soon", TODAY) is None


def test_empty_string_rejected():
    assert parse_date_phrase("", TODAY) is None
    assert parse_date_phrase("   ", TODAY) is None


def test_explicit_past_date_rejected():
    assert parse_date_phrase("01-01-2020", TODAY) is None


def test_implausibly_far_future_rejected():
    assert parse_date_phrase("30-08-2030", TODAY) is None  # >365 days out


def test_exactly_365_days_out_is_accepted():
    far = TODAY + dt.timedelta(days=365)
    assert parse_date_phrase(far.strftime("%d-%m-%Y"), TODAY) == far
