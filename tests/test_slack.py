"""Tests for the stdlib Slack bridge (`court.slack`). No network is used."""
import json
import urllib.error
from argparse import Namespace
from pathlib import Path

import pytest

from court import slack


class _Response:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    for name in ("COURT_SLACK_TOKEN", "COURT_SLACK_CHANNEL", "COURT_SLACK_NOTIFY_USER",
                 "COURT_SLACK_ENV_FILE", "SLACK_BOT_TOKEN", "XDG_STATE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("COURT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(slack, "_fallback_env_file_token", lambda repo, key: ("", ""))


def _git(repo, *argv):
    import subprocess
    return subprocess.run(["git", "-C", str(repo), *argv], check=True,
                          capture_output=True, text=True)


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "castle")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test Author")
    (repo / ".court" / "quests").mkdir(parents=True)
    (repo / ".court" / "LEDGER.md").write_text("- realm founded\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _add_quest(repo, qid="Q123-Held-Thing", status="HELD"):
    text = f"""---
id: {qid}
title: The held thing
kind: quest
status: {status}
branch: quest/{qid.split('-')[0].lower()}-held-thing
---

# {qid} — The held thing

## Castle Ledger
- **2026-11-01T00:00:00Z** — WORKING → HELD: waiting on auth model

## The Kingdom Requires
Deliver it.

## Expected Tribute
- [ ] it works

## Tribute Rendered
### Audience
None required.

## Audience Log
"""
    (repo / ".court" / "quests" / f"{qid}.md").write_text(text, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", f"charter {qid}")
    return qid


def _snap(*quests):
    return {"quests": list(quests)}


def _quest(qid, status="HELD", behind=0, title="The held thing"):
    return {
        "id": qid,
        "title": title,
        "kind": "quest",
        "status": status,
        "git_status": {"behind": behind},
        "pending_audience": False,
    }


def _frontier(qid, question="Which auth model?"):
    return [{
        "id": qid,
        "title": "The held thing",
        "status": "HELD",
        "kind": "blocked",
        "question": question,
        "next": "rule; a decree without an explicit status resumes it at WORKING",
    }]


def _install_http(monkeypatch, handler):
    calls = []

    def fake(req, timeout=None):
        method = req.full_url.rsplit("/", 1)[-1]
        payload = json.loads((req.data or b"{}").decode("utf-8"))
        calls.append((method, payload))
        result = handler(method, payload)
        if isinstance(result, Exception):
            raise result
        return _Response(result)

    monkeypatch.setattr(slack.urllib.request, "urlopen", fake)
    return calls


# ---------------------------------------------------------------------------
# Unconfigured
# ---------------------------------------------------------------------------


def test_unconfigured_status_and_sync_make_no_http_calls(tmp_path, monkeypatch, capsys):
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP called")))

    status = slack.render_status(repo)
    assert "not configured" in status
    assert "token: absent" in status

    result = slack.sync(repo)
    out = capsys.readouterr().out
    assert result["configured"] is False
    assert result["posted"] == []
    assert "not configured" in out
    assert "Nothing sent" in out


# ---------------------------------------------------------------------------
# Outbound dedupe
# ---------------------------------------------------------------------------


def test_second_sync_posts_nothing_and_changed_ruling_posts_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap(_quest("Q123-Held-Thing")))
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    frontier = {"items": _frontier("Q123-Held-Thing")}
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: list(frontier["items"]))
    monkeypatch.setattr(slack, "ingest_replies", lambda *a, **k: [])

    sent = []

    def fake_post(cfg, text, state=None, target=None):
        sent.append(text)
        return {"ts": f"100.{len(sent)}", "channel": "C012345", "target": "C012345"}

    monkeypatch.setattr(slack, "post_message", fake_post)

    slack.sync(repo)
    slack.sync(repo)
    assert len(sent) == 1

    frontier["items"] = _frontier("Q123-Held-Thing", question="Which token format?")
    slack.sync(repo)
    assert len(sent) == 2

    state = slack.load_state(repo)
    assert len(state["posted"]) == 2
    assert state["last_sync"]


def test_sync_dry_run_sends_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap(_quest("Q123-Held-Thing")))
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier("Q123-Held-Thing"))
    monkeypatch.setattr(slack, "post_message",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    monkeypatch.setattr(slack.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP called")))

    result = slack.sync(repo, dry_run=True)
    assert len(result["would_post"]) == 1
    assert result["posted"] == []
    assert not slack.state_path(repo).exists()


# ---------------------------------------------------------------------------
# Reply ingestion
# ---------------------------------------------------------------------------


def test_reply_becomes_one_validated_decree_and_is_consumed_once(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    qid = _add_quest(repo)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap(_quest(qid)))
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier(qid))
    monkeypatch.setattr(slack, "post_message",
                        lambda cfg, text, state=None, target=None:
                        {"ts": "100.1", "channel": "C012345", "target": "C012345"})

    def handler(method, payload):
        if method == "auth.test":
            return {"ok": True, "user": "courtbridge", "user_id": "UBOT"}
        assert method == "conversations.replies"
        return {"ok": True, "messages": [
            {"ts": "100.1", "text": "[DECISION] Q123-Held-Thing"},
            {"ts": "100.2", "user": "U02MA74K9DY", "text": "Use session cookies."},
        ]}

    calls = _install_http(monkeypatch, handler)

    first = slack.sync(repo)
    assert len(first["replies"]) == 1
    decrees = sorted((repo / ".court" / "decrees").glob("*.json"))
    assert len(decrees) == 1
    packet = json.loads(decrees[0].read_text(encoding="utf-8"))
    assert packet["by"] == "M'Lord"
    assert packet["source"] == "slack reply in C012345"
    assert packet["decisions"] == [{"quest": qid, "ruling": "Use session cookies."}]
    assert "100.2" in slack.load_state(repo)["consumed_replies"]

    second = slack.sync(repo)
    assert second["replies"] == []
    assert len(sorted((repo / ".court" / "decrees").glob("*.json"))) == 1
    assert sum(1 for method, _ in calls if method == "auth.test") == 2


def _reply_setup(tmp_path, monkeypatch, qid="Q123-Held-Thing"):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    _add_quest(repo, qid=qid)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap(_quest(qid)))
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier(qid))
    monkeypatch.setattr(slack, "post_message",
                        lambda cfg, text, state=None, target=None:
                        {"ts": "200.1", "channel": "C012345", "target": "C012345"})
    return repo


def test_invalid_arguments_falls_back_to_history_and_authors_once(tmp_path, monkeypatch):
    repo = _reply_setup(tmp_path, monkeypatch)

    def handler(method, payload):
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT"}
        if method == "conversations.replies":
            raise slack.SlackError("conversations.replies failed: invalid_arguments")
        assert method == "conversations.history"
        assert payload == {"channel": "C012345", "limit": 200, "oldest": "200.1"}
        return {"ok": True, "messages": [
            {"ts": "200.1", "text": "[DECISION] Q123-Held-Thing"},
            {"ts": "200.2", "user": "U02MA74K9DY", "text": "Use session cookies."},
        ]}

    _install_http(monkeypatch, handler)
    first = slack.sync(repo)
    assert len(first["replies"]) == 1
    decrees = sorted((repo / ".court" / "decrees").glob("*.json"))
    assert len(decrees) == 1
    packet = json.loads(decrees[0].read_text(encoding="utf-8"))
    assert packet["decisions"] == [{"quest": "Q123-Held-Thing",
                                    "ruling": "Use session cookies."}]

    second = slack.sync(repo)
    assert second["replies"] == []
    assert len(sorted((repo / ".court" / "decrees").glob("*.json"))) == 1


def test_bot_authored_messages_are_ignored(tmp_path, monkeypatch):
    repo = _reply_setup(tmp_path, monkeypatch)

    def handler(method, payload):
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT"}
        if method == "conversations.replies":
            return {"ok": True, "messages": [
                {"ts": "200.1", "text": "[DECISION] Q123-Held-Thing"},
                {"ts": "200.2", "user": "UBOT", "text": "bot echo"},
                {"ts": "200.3", "bot_id": "B99", "text": "bot webhook"},
            ]}
        raise AssertionError(method)

    _install_http(monkeypatch, handler)
    result = slack.sync(repo)
    assert result["replies"] == []
    assert sorted((repo / ".court" / "decrees").glob("*.json")) == []


def test_ambiguous_top_level_reply_with_two_unanswered_posts_is_skipped(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap())
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier_many(2))
    sent = []

    def fake_post(cfg, text, state=None, target=None):
        sent.append(text)
        return {"ts": f"300.{len(sent)}", "channel": "C012345", "target": "C012345"}

    monkeypatch.setattr(slack, "post_message", fake_post)

    def handler(method, payload):
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT"}
        assert method == "conversations.replies"
        return {"ok": True, "messages": [
            {"ts": payload["ts"], "text": "posted"},
            {"ts": "300.9", "user": "U02MA74K9DY", "text": "Which one?"},
        ]}

    _install_http(monkeypatch, handler)
    result = slack.sync(repo)
    assert result["replies"] == []
    assert sorted((repo / ".court" / "decrees").glob("*.json")) == []
    assert "ambiguous top-level reply" in capsys.readouterr().out


def test_threaded_reply_via_history_fallback_is_authored(tmp_path, monkeypatch):
    repo = _reply_setup(tmp_path, monkeypatch)

    def handler(method, payload):
        if method == "auth.test":
            return {"ok": True, "user_id": "UBOT"}
        if method == "conversations.replies":
            raise slack.SlackError("conversations.replies failed: invalid_arguments")
        assert method == "conversations.history"
        return {"ok": True, "messages": [
            {"ts": "200.1", "text": "[DECISION] Q123-Held-Thing"},
            {"ts": "200.2", "thread_ts": "200.1", "user": "U02MA74K9DY",
             "text": "Threaded ruling."},
        ]}

    _install_http(monkeypatch, handler)
    result = slack.sync(repo)
    assert len(result["replies"]) == 1
    decrees = sorted((repo / ".court" / "decrees").glob("*.json"))
    assert len(decrees) == 1
    packet = json.loads(decrees[0].read_text(encoding="utf-8"))
    assert packet["decisions"] == [{"quest": "Q123-Held-Thing",
                                    "ruling": "Threaded ruling."}]


def _frontier_many(count):
    return [{
        "id": f"Q{i:03d}-Item",
        "title": f"Item {i}",
        "status": "HELD",
        "kind": "blocked",
        "question": f"Decision {i}?",
        "next": "rule",
    } for i in range(count)]


# ---------------------------------------------------------------------------
# Blockers — only what the watch cannot clear mechanically
# ---------------------------------------------------------------------------


def test_fifty_drifted_working_quests_produce_no_outbound_items(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.decisions, "collect", lambda repo, snap=None, **kw: [])
    snap = _snap(*[_quest(f"Q{i:03d}-Drift", status="WORKING", behind=5)
                   for i in range(50)])
    assert slack.collect_items(repo, snap, {"quests": {}, "last_pickup": {}}) == []


def test_tribute_ready_escalated_conflict_is_exactly_one_blocker(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.decisions, "collect", lambda repo, snap=None, **kw: [])
    snap = _snap(_quest("Q123-Held-Thing", status="TRIBUTE_READY", behind=2))
    watch = {"quests": {"Q123-Held-Thing": {"merge_conflict": {
        "files": ["a.py"], "ts": 1, "escalated_ts": 2,
    }}}, "last_pickup": {}}

    items = slack.collect_items(repo, snap, watch)
    assert len(items) == 1
    assert items[0]["kind"] == "blocker"
    assert "Q123-Held-Thing" in items[0]["text"]
    assert "merge conflict" in items[0]["text"]
    assert "2 commit(s) behind castle" in items[0]["text"]


def test_pickup_rejection_needs_streak_three_or_a_remedy(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.decisions, "collect", lambda repo, snap=None, **kw: [])

    one = {"quests": {}, "last_pickup": {"rejected": "foreign path"},
           "pickup_streak": 1}
    assert slack.blocker_items(_snap(), one) == []

    three = {"quests": {}, "last_pickup": {
        "rejected": "foreign path", "remedy": "rebase the plea onto castle",
    }, "pickup_streak": 3}
    items = slack.blocker_items(_snap(), three)
    assert len(items) == 1
    assert "3 consecutive" in items[0]["text"]
    assert "rebase the plea onto castle" in items[0]["text"]


def test_a_standing_conflict_keeps_one_fingerprint_as_drift_grows(tmp_path, monkeypatch):
    """Board-snapshot commits move `behind` every tick; the blocker identity must
    not, or the same conflict is re-posted to Slack every few minutes."""
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.decisions, "collect", lambda repo, snap=None, **kw: [])
    watch = {"quests": {"Q123-Held-Thing": {"merge_conflict": {
        "files": ["a.py"], "ts": 1, "escalated_ts": 2,
    }}}, "last_pickup": {}}

    first = slack.collect_items(
        repo, _snap(_quest("Q123-Held-Thing", status="TRIBUTE_READY", behind=2)), watch)
    later = slack.collect_items(
        repo, _snap(_quest("Q123-Held-Thing", status="TRIBUTE_READY", behind=9)), watch)
    assert first[0]["fingerprint"] == later[0]["fingerprint"]


def test_a_standing_pickup_rejection_keeps_one_fingerprint_as_the_streak_grows():
    quests = {"quests": {}, "last_pickup": {"rejected": "invalid prerequisite 'None.'"}}
    three = slack.blocker_items(_snap(), dict(quests, pickup_streak=3))
    nine = slack.blocker_items(_snap(), dict(quests, pickup_streak=9))
    assert three[0]["fingerprint"] == nine[0]["fingerprint"]


def test_a_refused_channel_backs_off_instead_of_retrying_every_tick(tmp_path, monkeypatch):
    """If Slack stops accepting our posts (app messaging turned off, bot
    removed), the bridge must go quiet for a while — not re-attempt every tick."""
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap())
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier_many(2))
    monkeypatch.setattr(slack, "ingest_replies", lambda *a, **k: [])

    def _refuse(cfg, text, state=None):
        raise slack.SlackError("Sending messages to this app has been turned off")

    monkeypatch.setattr(slack, "post_message", _refuse)
    out = []
    slack.sync(repo, say=out.append)
    assert any("ERROR" in line for line in out)
    assert slack._in_post_backoff(slack.load_state(repo))

    # Next tick: nothing is attempted, and nothing is recorded as sent.
    monkeypatch.setattr(slack, "post_message",
                        lambda *a, **k: pytest.fail("must not post during backoff"))
    result = slack.sync(repo, say=out.append)
    assert result["posted"] == []


def test_eight_items_post_five_messages_plus_one_digest_then_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap())
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier_many(8))
    monkeypatch.setattr(slack, "ingest_replies", lambda *a, **k: [])

    sent = []

    def fake_post(cfg, text, state=None, target=None):
        sent.append(text)
        return {"ts": f"100.{len(sent)}", "channel": "C012345", "target": "C012345"}

    monkeypatch.setattr(slack, "post_message", fake_post)

    slack.sync(repo)
    assert len(sent) == 6
    assert "Q000-Item" in sent[0]
    digest = sent[-1]
    assert "[DIGEST] 3 more pipeline item(s)" in digest
    for qid in ("Q005-Item", "Q006-Item", "Q007-Item"):
        assert qid in digest
    assert len(slack.load_state(repo)["posted"]) == 8

    slack.sync(repo)
    assert len(sent) == 6


# ---------------------------------------------------------------------------
# JSON surface
# ---------------------------------------------------------------------------


def test_json_dry_run_emits_one_parseable_document(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COURT_SLACK_TOKEN", "xoxb-test-token")
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)
    monkeypatch.setattr(slack.nightwatch, "snapshot", lambda repo, timeout=0: _snap(_quest("Q123-Held-Thing")))
    monkeypatch.setattr(slack.nightwatch, "load_state", lambda repo: {"quests": {}, "last_pickup": {}})
    monkeypatch.setattr(slack.decisions, "collect",
                        lambda repo, snap=None, **kw: _frontier("Q123-Held-Thing"))
    monkeypatch.setattr(slack.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("HTTP called")))

    slack.cmd_slack(Namespace(action="sync", dry_run=True, json=True, repo=str(repo)))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["configured"] is True
    assert len(payload["would_post"]) == 1
    assert payload["posted"] == []
    assert "Slack sync (dry run)" not in out


# ---------------------------------------------------------------------------
# Secret handling
# ---------------------------------------------------------------------------


def test_token_never_appears_on_the_error_path(tmp_path, monkeypatch, capsys):
    token = "xoxb-very-secret-token"
    monkeypatch.setenv("COURT_SLACK_TOKEN", token)
    monkeypatch.setenv("COURT_SLACK_CHANNEL", "C012345")
    repo = _repo(tmp_path)

    _install_http(monkeypatch, lambda method, payload:
                  urllib.error.URLError(f"boom {token}"))

    slack.cmd_slack(Namespace(action="status", probe=True, repo=str(repo)))
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert token not in combined
    assert "***" in combined
    assert "FAILED" in combined
