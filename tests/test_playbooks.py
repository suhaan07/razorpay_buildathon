import pytest

from app.playbooks.loader import PlaybookLoadError, load_playbooks
from app.playbooks.renderer import MissingTokenError, render


def test_real_playbook_loads_and_validates():
    registry = load_playbooks()
    assert "receivables_escalation" in registry
    playbook = registry["receivables_escalation"]
    assert [lvl["recipients"] for lvl in playbook["levels"]] == ["spoc", "manager", "skip_level", "customer"]
    assert playbook["levels"][-1]["channel"] == "voice"
    assert playbook["onExhausted"]["status"] == "exhausted"


def test_invalid_playbook_reports_the_offending_field(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"name": "x", "version": 1, "type": "not-a-real-type", "levels": [], "onExhausted": {"status": "x"}}')
    with pytest.raises(PlaybookLoadError) as excinfo:
        load_playbooks(tmp_path)
    assert "bad.json" in str(excinfo.value)


def test_render_substitutes_tokens():
    template = "Hi {{customer_name}}, pay {{amount}} here: {{pay_link}}"
    result = render(template, {"customer_name": "Acme", "amount": "₹1,000.00", "pay_link": "https://rzp.io/l/x"})
    assert result == "Hi Acme, pay ₹1,000.00 here: https://rzp.io/l/x"


def test_render_raises_on_missing_token():
    with pytest.raises(MissingTokenError):
        render("Hi {{customer_name}}", {})
