from app.decisions.decision_layer import MANAGER_THRESHOLD, SKIP_LEVEL_THRESHOLD, decide


def _ctx(bucket="0-15", score=100.0, outstanding=10_000.0, touch_count=0, **extra):
    base = {
        "bucket": bucket,
        "reliability_score": score,
        "outstanding_amount": outstanding,
        "touch_count": touch_count,
    }
    base.update(extra)
    return base


def test_decide_is_deterministic():
    ctx = _ctx(bucket="16-30", score=60, touch_count=2)
    first = decide(ctx)
    second = decide(ctx)
    assert first.suggested_level == second.suggested_level
    assert first.wait_days == second.wait_days
    assert first.urgency_score == second.urgency_score


def test_decide_starts_at_spoc_for_early_bucket_and_good_score():
    result = decide(_ctx(bucket="0-15", score=100))
    assert result.suggested_level == 0
    assert result.urgency_score < MANAGER_THRESHOLD


def test_decide_jumps_to_skip_level_for_very_late_bucket():
    result = decide(_ctx(bucket="90+", score=100))
    assert result.suggested_level == 2
    assert result.urgency_score >= SKIP_LEVEL_THRESHOLD


def test_decide_never_suggests_voice():
    # voice (index 3) is reached only mechanically, after skip_level's wait
    # elapses unpaid — decide() must never suggest jumping straight to it,
    # no matter how extreme the signals.
    result = decide(_ctx(bucket="90+", score=0, outstanding=10_000_000.0, touch_count=99))
    assert result.suggested_level <= 2


def test_decide_urgency_increases_as_bucket_worsens():
    scores = [decide(_ctx(bucket=b)).urgency_score for b in ["Not Due", "0-15", "16-30", "31-60", "61-90", "90+"]]
    assert scores == sorted(scores)


def test_decide_wait_days_shrinks_as_urgency_rises():
    calm = decide(_ctx(bucket="0-15", score=100))
    urgent = decide(_ctx(bucket="90+", score=0, outstanding=1_000_000.0, touch_count=10))
    assert urgent.wait_days < calm.wait_days


def test_decide_combined_signals_tip_a_mid_bucket_case_to_manager():
    # none of these alone crosses the manager threshold, but combined they do
    result = decide(_ctx(bucket="16-30", score=40, outstanding=350_000.0, touch_count=4))
    assert result.suggested_level >= 1
