"""Tests for `court ui-signoff`: chat assent becomes a record a gate can read."""
from argparse import Namespace

from court import convoy_guard as cg
from court import store
from court.cli import cmd_ui_signoff
from court.models import Quest


def _quest(court_dir, ui_line="- **UI Review:** PENDING (awaiting the Court Artist)"):
    quest = Quest(
        id="Q1-Core-Assent",
        title="Assent",
        kind="quest",
        app="core",
        concern="assent",
        status="GATE",
        body_sections={"Master of Coin's Audit": ui_line},
    )
    store.save(quest, court_root=court_dir, auto_commit=False)
    return quest


def _args(**kw):
    base = dict(quest_id="Q1-Core-Assent", by="M'Lord", note="approved in chat",
                waive=False, none_required=False, no_commit=True)
    base.update(kw)
    return Namespace(**base)


def _court_dir(tmp_path):
    court_dir = tmp_path / ".court"
    (court_dir / "quests").mkdir(parents=True)
    return court_dir


def test_approval_replaces_the_pending_line_and_records_signoff(tmp_path, monkeypatch):
    court_dir = _court_dir(tmp_path)
    monkeypatch.setenv("COURT_DIR", str(court_dir))
    _quest(court_dir)

    cmd_ui_signoff(_args())

    loaded = store.load("Q1-Core-Assent", court_root=court_dir)
    audit = loaded.body_sections["Master of Coin's Audit"]
    assert "APPROVED" in audit and "signed off by M'Lord" in audit
    assert "UI Sign-off" in audit and "approved in chat" in audit
    state, detail = cg.ui_assent_state(tmp_path, "Q1-Core-Assent")
    assert state == "signed", detail


def test_a_waiver_is_recorded_and_reads_as_waived(tmp_path, monkeypatch):
    court_dir = _court_dir(tmp_path)
    monkeypatch.setenv("COURT_DIR", str(court_dir))
    _quest(court_dir)

    cmd_ui_signoff(_args(waive=True, note="skip-ui-review, backend-only ship"))

    loaded = store.load("Q1-Core-Assent", court_root=court_dir)
    assert "WAIVED by M'Lord" in loaded.body_sections["Master of Coin's Audit"]
    assert cg.ui_assent_state(tmp_path, "Q1-Core-Assent")[0] in ("signed", "waived")


def test_none_required_declaration_is_recorded(tmp_path, monkeypatch):
    court_dir = _court_dir(tmp_path)
    monkeypatch.setenv("COURT_DIR", str(court_dir))
    _quest(court_dir, ui_line="- **UI Review:** None required (backend only)")

    cmd_ui_signoff(_args(none_required=True, note="headless only"))

    state, detail = cg.ui_assent_state(tmp_path, "Q1-Core-Assent")
    assert state == "signed", detail
