"""Unit tests for vigils — the bounded Steward hand-off to the Night's Watch."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from court import vigils


def _repo(tmp_path):
    (tmp_path / ".court").mkdir(exist_ok=True)
    return tmp_path


def _spec(**kw):
    base = dict(
        id="q234-after-q123",
        when=["quest-status:Q123=READY_TO_RAZE"],
        then="dispatch:Q234",
        reason="Q234 needs Q123's schema on castle before dispatch.",
        expires="6h",
    )
    base.update(kw)
    return vigils.build(**base)


# ---------------------------------------------------------------------------
# Narrowness contract
# ---------------------------------------------------------------------------


def test_reason_is_mandatory():
    with pytest.raises(vigils.VigilError):
        _spec(reason="meh")


def test_expiry_is_mandatory_and_capped():
    with pytest.raises(vigils.VigilError):
        _spec(expires="")
    with pytest.raises(vigils.VigilError):
        _spec(expires="30d")


def test_expiry_in_the_past_is_rejected():
    with pytest.raises(vigils.VigilError):
        _spec(expires="2020-01-01T00:00:00Z")


def test_max_fires_is_bounded():
    with pytest.raises(vigils.VigilError):
        _spec(max_fires=0)
    with pytest.raises(vigils.VigilError):
        _spec(max_fires=99)


def test_at_least_one_condition_required():
    with pytest.raises(vigils.VigilError):
        _spec(when=[])


def test_destructive_actions_are_not_allowed():
    for then in ("ship", "raze:Q1", "pillory:Q1", "patchwork:Q1"):
        with pytest.raises(vigils.VigilError):
            _spec(then=then)


def test_unknown_condition_is_rejected():
    with pytest.raises(vigils.VigilError):
        _spec(when=["the vibes are good"])


def test_action_grammar_maps_to_court_argv():
    assert vigils.action_argv("dispatch:Q234") == (["dispatch", "Q234", "--standup"], True)
    assert vigils.action_argv("goad:Q1") == (["goad", "Q1"], True)
    assert vigils.action_argv("coin:Q1") == (["coin", "Q1"], True)
    assert vigils.action_argv("collect:Q1") == (["collect", "Q1", "--standup"], True)
    assert vigils.action_argv("collect") == (["collect", "--standup"], True)
    assert vigils.action_argv("advance:Q1=TRIBUTE_READY") == (["advance", "Q1", "TRIBUTE_READY"], False)
    assert vigils.action_argv("note:just tell me") == ([], False)


def test_advance_status_must_be_real():
    with pytest.raises(vigils.VigilError):
        _spec(then="advance:Q1=SOMEWHERE")


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def test_quest_status_condition(tmp_path):
    repo = _repo(tmp_path)
    quests = {"Q123-X": {"id": "Q123-X", "status": "READY_TO_RAZE"}}
    ok, why = vigils.check_condition("quest-status:Q123=READY_TO_RAZE", repo, quests)
    assert ok and "READY_TO_RAZE" in why
    ok, why = vigils.check_condition("quest-status:Q123=WORKING", repo, quests)
    assert not ok
    ok, _ = vigils.check_condition("quest-status:Q123!=WORKING", repo, quests)
    assert ok
    ok, why = vigils.check_condition("quest-status:Q999=WORKING", repo, quests)
    assert not ok and "not found" in why


def test_quest_status_prefix_match(tmp_path):
    repo = _repo(tmp_path)
    quests = {"Q123-Long-Slug": {"id": "Q123-Long-Slug", "status": "DONE"}}
    ok, _ = vigils.check_condition("quest-status:Q123=DONE", repo, quests)
    assert ok


def test_file_exists_condition(tmp_path):
    repo = _repo(tmp_path)
    (repo / "artifact.txt").write_text("x", encoding="utf-8")
    assert vigils.check_condition("file-exists:artifact.txt", repo, {})[0]
    assert not vigils.check_condition("file-exists:missing.txt", repo, {})[0]
    assert not vigils.check_condition("file-exists:../../etc/hosts", repo, {})[0]


def test_after_condition(tmp_path):
    repo = _repo(tmp_path)
    past = vigils.iso(vigils.now_utc() - timedelta(minutes=1))
    future = vigils.iso(vigils.now_utc() + timedelta(hours=1))
    assert vigils.check_condition(f"after:{past}", repo, {})[0]
    assert not vigils.check_condition(f"after:{future}", repo, {})[0]


# ---------------------------------------------------------------------------
# Evaluation / lifecycle
# ---------------------------------------------------------------------------


def test_ready_vigils_fires_once_then_retires(tmp_path):
    repo = _repo(tmp_path)
    spec = _spec()
    vigils.save(repo, spec)
    quests = {"Q123-X": {"id": "Q123-X", "status": "READY_TO_RAZE"}}
    ready, notes = vigils.ready_vigils(repo, quests)
    assert [s["id"] for s, _ in ready] == ["q234-after-q123"]

    vigils.mark_fired(repo, spec, {"ok": True, "exit": 0, "seconds": 1.0})
    assert vigils.load_all(repo) == []                       # retired
    done = vigils.load_all(repo, include_finished=True)
    assert done and done[0]["status"] == "fired"
    assert len(done[0]["fires"]) == 1


def test_expired_vigil_is_retired_not_fired(tmp_path):
    repo = _repo(tmp_path)
    spec = _spec(expires="6h")
    spec["expires_at"] = vigils.iso(vigils.now_utc() - timedelta(minutes=1))
    vigils.save(repo, spec)
    ready, notes = vigils.ready_vigils(repo, {})
    assert ready == []
    assert any(n["level"] == "expired" for n in notes)
    assert vigils.load_all(repo, include_finished=True)[0]["status"] == "expired"


def test_unsatisfied_condition_keeps_the_vigil_active(tmp_path):
    repo = _repo(tmp_path)
    vigils.save(repo, _spec())
    ready, _ = vigils.ready_vigils(repo, {"Q123-X": {"id": "Q123-X", "status": "WORKING"}})
    assert ready == []
    assert len(vigils.load_all(repo)) == 1


def test_multiple_conditions_are_anded(tmp_path):
    repo = _repo(tmp_path)
    spec = _spec(when=["quest-status:Q123=READY_TO_RAZE", "quest-status:Q234=CHARTERED"])
    vigils.save(repo, spec)
    quests = {"Q123-X": {"id": "Q123-X", "status": "READY_TO_RAZE"},
              "Q234-Y": {"id": "Q234-Y", "status": "PLANNED"}}
    ready, _ = vigils.ready_vigils(repo, quests)
    assert ready == []
    quests["Q234-Y"]["status"] = "CHARTERED"
    ready, _ = vigils.ready_vigils(repo, quests)
    assert [s["id"] for s, _ in ready] == ["q234-after-q123"]


def test_roundtrip_on_disk_is_json(tmp_path):
    repo = _repo(tmp_path)
    p = vigils.save(repo, _spec())
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["id"] == "q234-after-q123"
    assert data["then"] == "dispatch:Q234"
