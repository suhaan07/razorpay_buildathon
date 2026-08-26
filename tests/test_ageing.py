import datetime as dt

from app.data.ageing import bucket_for, days_overdue, is_due_this_week, is_overdue


def test_not_due_when_due_date_in_future():
    today = dt.date(2026, 8, 21)
    assert bucket_for(today + dt.timedelta(days=5), today) == "Not Due"


def test_due_today_is_not_overdue():
    today = dt.date(2026, 8, 21)
    assert not is_overdue(today, today)
    assert bucket_for(today, today) == "Not Due"


def test_unclassified_when_due_date_missing():
    today = dt.date(2026, 8, 21)
    assert bucket_for(None, today) == "Unclassified"
    assert days_overdue(None, today) == 0


def test_bucket_boundaries():
    today = dt.date(2026, 8, 21)
    cases = {
        1: "0-15",
        15: "0-15",
        16: "16-30",
        30: "16-30",
        31: "31-60",
        60: "31-60",
        61: "61-90",
        90: "61-90",
        91: "90+",
        200: "90+",
    }
    for overdue_days, expected_bucket in cases.items():
        due_date = today - dt.timedelta(days=overdue_days)
        assert bucket_for(due_date, today) == expected_bucket, overdue_days
        assert days_overdue(due_date, today) == overdue_days


def test_due_this_week_window():
    monday = dt.date(2026, 8, 17)  # a Monday
    friday = monday + dt.timedelta(days=4)
    saturday = monday + dt.timedelta(days=5)
    today = dt.date(2026, 8, 19)  # Wednesday of that week
    assert is_due_this_week(monday, today)
    assert is_due_this_week(friday, today)
    assert not is_due_this_week(saturday, today)
