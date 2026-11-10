"""Tests for sequenced `court ship --confirm --stage ...` deployment."""
import subprocess
from argparse import Namespace

from court import cli


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def _rev(cwd, ref):
    return _git(cwd, "rev-parse", ref).stdout.strip()


def _repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    repo = tmp_path / "work"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "ship@test")
    _git(repo, "config", "user.name", "ship")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    _git(repo, "push", "-u", "origin", "main")

    _git(repo, "checkout", "-b", "castle")
    (repo / "feature.txt").write_text("feature\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "castle feature")
    _git(repo, "checkout", "main")
    return origin, repo


def _run(monkeypatch, repo, stages):
    monkeypatch.setattr(cli, "_ship_find_main_checkout", lambda base: repo)
    args = Namespace(dry_run=False)
    return cli._execute_ship_deployment(args, "main", "castle", quests=[], stages=stages)


def test_default_stages_are_the_original_one_shot(tmp_path):
    assert cli._resolve_ship_stages(None) == ["merge", "push", "stamp"]


def test_stages_run_in_canonical_order_and_accept_comma_lists(tmp_path):
    assert cli._resolve_ship_stages(["push", "merge"]) == ["merge", "push"]
    assert cli._resolve_ship_stages(["stamp,merge", "push"]) == ["merge", "push", "stamp"]
    assert cli._resolve_ship_stages(["interrogate"]) == ["interrogate"]


def test_an_unknown_stage_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        cli._resolve_ship_stages(["deploy"])


def test_merge_stage_lands_the_merge_but_does_not_push(tmp_path, monkeypatch):
    origin, repo = _repo(tmp_path)
    before = _rev(origin, "main")

    assert _run(monkeypatch, repo, ["merge"]) == 0

    assert _rev(repo, "main") == _rev(repo, "castle")
    assert _rev(origin, "main") == before, "merge-only must not trigger a remote deploy"


def test_push_stage_refuses_when_the_merge_has_not_happened(tmp_path, monkeypatch):
    origin, repo = _repo(tmp_path)
    before = _rev(origin, "main")

    assert _run(monkeypatch, repo, ["push"]) == 1

    assert _rev(origin, "main") == before


def test_merge_then_push_sequences_the_pipeline(tmp_path, monkeypatch):
    origin, repo = _repo(tmp_path)

    assert _run(monkeypatch, repo, ["merge"]) == 0
    assert _run(monkeypatch, repo, ["push"]) == 0

    assert _rev(origin, "main") == _rev(repo, "main")
    assert _git(repo, "merge-base", "--is-ancestor", "castle", "main").returncode == 0


def test_interrogation_only_never_touches_production(tmp_path, monkeypatch):
    origin, repo = _repo(tmp_path)
    before = _rev(origin, "main")

    # No mutating stage: it must not even require a production checkout.
    monkeypatch.setattr(cli, "_ship_find_main_checkout", lambda base: None)
    args = Namespace(dry_run=False)
    assert cli._execute_ship_deployment(args, "main", "castle", quests=[], stages=["interrogate"]) == 0
    assert _rev(origin, "main") == before
