"""Tests for the deterministic AI-index and punished-successtor-audit commands."""
from pathlib import Path

from court import index as ai_index
from court import punished_audit
from court.models import Quest

_GOAL = "## The Kingdom Requires\nDo the thing.\n\n## Expected Tribute\n- ship it\n"


def _write_quest(court_root: Path, qid: str, path_dir: str, **fields) -> Quest:
    q = Quest(
        id=qid,
        title=fields.get("title", "Some Quest"),
        kind=fields.get("kind", "quest"),
        status=fields.get("status", "PUNISHED"),
        body_sections={"The Kingdom Requires": _GOAL},
    )
    killer = {"pilloried_by": fields.get("pilloried_by", "")}
    if "judgement" in fields:
        q.body_sections["Judgement of the Condemned"] = (
            "- **Reason:** failed audit\n"
            f"- **Successor Quest:** {fields['judgement']}\n"
        )
    body = f"---\nid: {qid}\ntitle: {q.title}\nkind: {q.kind}\nstatus: {q.status}\n"
    for k, v in killer.items():
        body += f"{k}: {v}\n"
    body += "---\n\n" + _GOAL
    (court_root / path_dir / f"{qid}.md").parent.mkdir(parents=True, exist_ok=True)
    (court_root / path_dir / f"{qid}.md").write_text(body, encoding="utf-8")
    return q


def _repos(tmp_path):
    repo = Path(tmp_path)
    court = repo / ".court"
    for d in ("quests", "epics", "archive"):
        (court / d).mkdir(parents=True, exist_ok=True)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    return repo, court


def test_punished_audit_flags_missing_and_not_advanced(tmp_path):
    repo, court = _repos(tmp_path)
    _write_quest(court, "Q100-Main-Thing", "quests", status="PUNISHED")
    _write_quest(court, "Q101-Has-Successor", "quests", status="PUNISHED",
                 pilloried_by="Q200-Successor-Quest")
    _write_quest(court, "Q200-Successor-Quest", "quests", status="PLANNED",
                 kind="quest")
    _write_quest(court, "Q102-Successor-Done", "quests", status="PUNISHED",
                 pilloried_by="Q201-Archived-Successor")
    _write_quest(court, "Q201-Archived-Successor", "archive", status="READY_TO_RAZE")

    data = punished_audit.audit(repo)
    assert data["punished_total"] == 3
    assert data["no_successor"] == 1
    assert "Q100-Main-Thing" in data["flags"]["no_successor"]
    not_adv = {r["punished"] for r in data["flags"]["successor_not_advanced"]}
    assert not_adv == {"Q101-Has-Successor"}


def test_punished_audit_pending_judgement_counts_as_no_successor(tmp_path):
    repo, court = _repos(tmp_path)
    _write_quest(court, "Q103-Pending-Marker", "quests", status="PUNISHED",
                 judgement="(pending \u2014 charter one now)")
    data = punished_audit.audit(repo)
    row = data["rows"][0]
    assert row["no_successor"] is True
    assert row["successor"] == ""


def test_ai_index_snapshot_has_freshness_header(tmp_path):
    repo, court = _repos(tmp_path)
    _write_quest(court, "Q110-Working", "quests", status="WORKING")
    _write_quest(court, "Q111-Ready", "quests", status="TRIBUTE_READY")
    snap = ai_index.build_snapshot(repo)
    assert snap["quests"]["by_status"]["WORKING"] == 1
    assert "freshness" in snap
    md = ai_index.render_markdown(snap)
    assert "last known snapshot" in md
    assert "generated_at" in md
    assert "AI Index" in md
    assert snap["repo_name"] in md