"""Composer default persona (court.ui_server): the default agent-type follows the
repo's own kilo.json `default_agent` — a Court-managed repo keeps defaulting to
`steward` while a plain source repo defaults to `code`. Pure reads of a
throwaway repo layout; never the real repos."""

import json

import pytest

from court import ui_server as u


def _make_repo(tmp_path, name="repo-a", default_agent=None):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    if default_agent is not None:
        (root / "kilo.json").write_text(
            json.dumps({"$schema": "https://app.kilo.ai/config.json",
                        "default_agent": default_agent}),
            encoding="utf-8")
    return root


def test_default_agent_read_from_repo_kilo_json(tmp_path):
    root = _make_repo(tmp_path, default_agent="code")
    assert u._repo_default_agent(str(root)) == "code"


def test_court_repo_defaults_to_steward(tmp_path):
    root = _make_repo(tmp_path, default_agent="steward")
    assert u._repo_default_agent(str(root)) == "steward"


def test_missing_kilo_json_yields_empty(tmp_path):
    root = _make_repo(tmp_path)
    assert u._repo_default_agent(str(root)) == ""


def test_unknown_agent_value_is_ignored(tmp_path):
    root = _make_repo(tmp_path, default_agent="emperor")
    assert u._repo_default_agent(str(root)) == ""


def test_malformed_kilo_json_yields_empty(tmp_path):
    root = _make_repo(tmp_path)
    (root / "kilo.json").write_text("{not json", encoding="utf-8")
    assert u._repo_default_agent(str(root)) == ""


def test_cache_honours_ttl(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, default_agent="code")
    monkeypatch.setattr(u, "_DEFAULT_AGENT_CACHE", {})
    first = u._repo_default_agent(str(root))
    # rewrite the file without bumping the cache clock: the stale answer stays
    (root / "kilo.json").write_text(
        json.dumps({"default_agent": "scout"}), encoding="utf-8")
    assert u._repo_default_agent(str(root)) == first


def test_repos_payload_carries_default_agent(tmp_path, monkeypatch):
    root = _make_repo(tmp_path, name="engine", default_agent="code")
    monkeypatch.setattr(u, "COURT_DIR", str(root))
    monkeypatch.setattr(u, "_DEFAULT_AGENT_CACHE", {})
    repos = u._repos()
    assert repos, "engine repo should always be discoverable"
    engine = [r for r in repos if r["root"] == str(root)][0]
    assert engine["default_agent"] == "code"
