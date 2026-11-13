"""Tests for the Court-driven Fly deploy: app/image manifest, flyctl argv,
the pre-deploy drain hook, and re-runnable image references.

Fly's GitHub integration is out of the deploy path (M'Lord, 2026-11-13);
`court ship --confirm --deploy` drives flyctl directly. These tests pin the
manifest resolution, the exact argv each stage runs, the science-image guard,
and the failure-visibility/exit-status contract -- without ever running flyctl.
"""
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from court import cli
from court import ship_deploy as sd


# --------------------------------------------------------------------------
# Fixtures: a production checkout carrying pb-app's four Fly configs
# --------------------------------------------------------------------------

# filename -> (app, dockerfile, release_command, settings module)
_APPS = {
    "fly.toml": ("pb-app-web", "Dockerfile.web", "python manage.py migrate_locked", "core.settings.web"),
    "fly-worker.toml": ("pb-app-worker", "Dockerfile.worker", "python manage.py migrate_locked", "core.settings.worker"),
    "fly-worker-heavy.toml": ("pb-app-worker-heavy", "Dockerfile.worker", None, "core.settings.worker"),
    "fly-worker-light.toml": ("pb-app-worker-light", "Dockerfile.worker-light", None, "core.settings.worker"),
}


def _write_config(root: Path, name: str, app: str, dockerfile: str, release=None, settings="core.settings.web"):
    lines = [f'app = "{app}"', "", "[deploy]", '  strategy = "rolling"']
    if release:
        lines += [f'  release_command = "{release}"', '  release_command_timeout = "900s"']
    lines += ["", "[build]", f'  dockerfile = "{dockerfile}"', "", "[env]",
              f'  DJANGO_SETTINGS_MODULE = "{settings}"', ""]
    (root / name).write_text("\n".join(lines), encoding="utf-8")


def _write_all(root: Path):
    for name, (app, dockerfile, release, settings) in _APPS.items():
        _write_config(root, name, app, dockerfile, release, settings)


# --------------------------------------------------------------------------
# App / image manifest resolution
# --------------------------------------------------------------------------

def test_fly_configs_include_worker_light():
    assert sd.FLY_CONFIGS["worker-light"] == "fly-worker-light.toml"
    assert set(sd.FLY_CONFIGS) == {"web", "worker", "worker-heavy", "worker-light"}


def test_manifest_routes_each_app_to_its_image():
    assert sd.image_for_app("pb-app-web") == "app"
    assert sd.image_for_app("pb-app-worker-light") == "app"   # -> the APP image, never science
    assert sd.image_for_app("pb-app-worker") == "science"
    assert sd.image_for_app("pb-app-worker-heavy") == "science"
    assert sd.image_for_app("pb-app-not-an-app") is None


def test_worker_light_is_an_app_image_member_only():
    assert "pb-app-worker-light" in sd.apps_for_image("app")
    assert "pb-app-worker-light" not in sd.apps_for_image("science")
    assert "pb-app-worker-light" in sd.SCIENCE_FORBIDDEN_APPS


def test_image_resolves_from_each_apps_own_fly_config(tmp_path):
    _write_all(tmp_path)
    records, errors = sd.resolve_apps(tmp_path)
    assert errors == []
    by_app = {r["app"]: r for r in records}
    assert by_app["pb-app-web"]["image"] == "app"
    assert by_app["pb-app-worker-light"]["image"] == "app"
    assert by_app["pb-app-worker"]["image"] == "science"
    assert by_app["pb-app-worker-heavy"]["image"] == "science"
    assert by_app["pb-app-worker-light"]["dockerfile"] == "Dockerfile.worker-light"
    # role is read off the config: workers drain, web does not
    assert by_app["pb-app-worker-light"]["role"] == "worker"
    assert by_app["pb-app-web"]["role"] == "web"
    # release commands are per-app and untouched (only web + worker migrate)
    assert by_app["pb-app-web"]["release_command"] == "python manage.py migrate_locked"
    assert by_app["pb-app-worker"]["release_command"] == "python manage.py migrate_locked"
    assert by_app["pb-app-worker-light"]["release_command"] is None
    assert by_app["pb-app-worker-heavy"]["release_command"] is None


def test_routing_survives_the_planned_image_consolidation(tmp_path):
    # Post-Q1487, worker-light's config points at the app image's Dockerfile.
    _write_all(tmp_path)
    _write_config(tmp_path, "fly-worker-light.toml", "pb-app-worker-light", "Dockerfile.web",
                  settings="core.settings.worker")
    records, errors = sd.resolve_apps(tmp_path)
    assert errors == []
    by_app = {r["app"]: r for r in records}
    assert by_app["pb-app-worker-light"]["image"] == "app"
    assert by_app["pb-app-worker-light"]["dockerfile"] == "Dockerfile.web"


def test_a_config_pointing_at_the_wrong_image_is_refused(tmp_path):
    _write_all(tmp_path)
    # worker-light's config drifted to the science dockerfile -> hard error, and
    # the app is withheld from the plan (any error refuses the whole run).
    _write_config(tmp_path, "fly-worker-light.toml", "pb-app-worker-light", "Dockerfile.worker",
                  settings="core.settings.worker")
    records, errors = sd.resolve_apps(tmp_path)
    assert "pb-app-worker-light" not in {r["app"] for r in records}
    assert any("pb-app-worker-light" in e and "science" in e for e in errors)


def test_selection_limits_the_plan(tmp_path):
    _write_all(tmp_path)
    records, errors = sd.resolve_apps(tmp_path, ["worker-light"])
    assert errors == []
    assert [r["app"] for r in records] == ["pb-app-worker-light"]


def test_unknown_fly_app_alias_is_refused(tmp_path):
    _write_all(tmp_path)
    records, errors = sd.resolve_apps(tmp_path, ["nope"])
    assert records == []
    assert any("unknown --fly-app" in e for e in errors)


# --------------------------------------------------------------------------
# The science-image guard: no path releases it to pb-app-worker-light
# --------------------------------------------------------------------------

def test_guard_refuses_science_for_worker_light():
    with pytest.raises(sd.DeployError):
        sd.guard_image_delivery("pb-app-worker-light", "science")
    # allowed deliveries do not raise
    sd.guard_image_delivery("pb-app-worker-light", "app")
    sd.guard_image_delivery("pb-app-worker", "science")


def test_deploy_stage_never_hands_worker_light_the_science_ref(tmp_path, monkeypatch):
    """End-to-end: with flyctl faked, the argv for worker-light carries the app
    image ref, and the science ref never appears on that app's command line."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path)
    refs = {"app": "registry.fly.io/pb-app-web:app-c2356b213f00",
            "science": "registry.fly.io/pb-app-worker:science-c2356b213f00"}
    seen = []

    def fake_flyctl(argv, cwd, timeout):
        seen.append(argv)
        return {"ok": True, "exit_code": 0, "stdout": "Released v9\n", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake_flyctl)
    failed = cli._ship_deploy_apps(tmp_path, "c2356b213f00" * 2, records, refs, False, None)
    assert failed is False
    worker_light = [a for a in seen if "fly-worker-light.toml" in a]
    assert worker_light, "worker-light release did not run"
    assert any("registry.fly.io/pb-app-web:app-" in arg for arg in worker_light[0])
    assert not any("science" in arg for arg in worker_light[0])


# --------------------------------------------------------------------------
# flyctl argv builders
# --------------------------------------------------------------------------

def test_build_argv_is_build_only_push_labelled_by_the_commit():
    assert sd.image_label("c2356b213f00deadbeef", "app") == "app-c2356b213f00"
    label = sd.image_label("c2356b213f00deadbeef", "app")
    assert sd.build_argv("app", "fly.toml", label) == [
        "flyctl", "deploy", "--build-only", "--push",
        "--image-label", "app-c2356b213f00", "-c", "fly.toml", "--yes",
    ]


def test_deploy_argv_releases_a_prebuilt_image_with_no_rebuild():
    argv = sd.deploy_argv("fly-worker-light.toml", "registry.fly.io/pb-app-web:app-c2356b213f00")
    assert argv == [
        "flyctl", "deploy", "--image", "registry.fly.io/pb-app-web:app-c2356b213f00",
        "-c", "fly-worker-light.toml", "--yes",
    ]
    assert "--build-only" not in argv and "--push" not in argv


def test_image_label_is_deterministic_per_commit_and_image():
    assert sd.image_label("abc123def456", "app") != sd.image_label("abc123def456", "science")
    assert sd.image_label("abc123def456", "app") == sd.image_label("abc123def456", "app")


def test_image_ref_is_deterministic_from_the_build_config(tmp_path):
    _write_all(tmp_path)
    assert sd.image_ref("app", tmp_path, "app-abc123") == "registry.fly.io/pb-app-web:app-abc123"
    assert sd.image_ref("science", tmp_path, "science-abc123") == "registry.fly.io/pb-app-worker:science-abc123"


def test_drain_argv_runs_the_management_command_in_the_app_fleet():
    assert sd.drain_argv("pb-app-worker", "drain_worker_fleet") == [
        "flyctl", "ssh", "console", "-a", "pb-app-worker",
        "-C", "python manage.py drain_worker_fleet",
    ]


def test_parse_image_ref_and_release_version():
    out = "image: registry.fly.io/pb-app-web:app-c2356b213f00 pushed\nReleased v1234\n"
    assert sd.parse_image_ref(out, "fallback") == "registry.fly.io/pb-app-web:app-c2356b213f00"
    assert sd.parse_image_ref("no reference here", "fallback") == "fallback"
    assert sd.parse_release_version(out) == "v1234"
    assert sd.parse_release_version("nothing useful") is None


def test_images_for_records_is_deduped_in_manifest_order(tmp_path):
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path)
    assert sd.images_for_records(records) == ["app", "science"]
    # a one-app selection only needs that app's image
    one, _ = sd.resolve_apps(tmp_path, ["worker-light"])
    assert sd.images_for_records(one) == ["app"]


# --------------------------------------------------------------------------
# Drain hook: absent -> fail-open (logged), present -> found
# --------------------------------------------------------------------------

def test_drain_command_absent_resolves_to_none(tmp_path):
    assert sd.find_drain_command(tmp_path, "drain_worker_fleet") is None


def test_drain_command_present_is_found(tmp_path):
    cmd = tmp_path / "apps" / "tasks" / "management" / "commands" / "drain_worker_fleet.py"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("from django.core.management.base import BaseCommand\n", encoding="utf-8")
    assert sd.find_drain_command(tmp_path, "drain_worker_fleet") == cmd


def test_missing_drain_hook_logs_and_continues(tmp_path, capsys):
    record = {"app": "pb-app-worker", "role": "worker"}
    assert cli._ship_pre_deploy_drain(tmp_path, record, "drain_worker_fleet", False) == "absent"
    out = capsys.readouterr().out
    assert "not present" in out and "fail-open" in out


# --------------------------------------------------------------------------
# Durable image refs: the re-runnability fix for the main == origin/main trap
# --------------------------------------------------------------------------

def test_recorded_ref_enables_a_later_deploy_with_no_build(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    assert sd.deployed_ref(repo, "deadbeef", "app") is None
    sd.record_image_ref(repo, "deadbeef", "app", "registry.fly.io/pb-app-web:app-deadbeef")
    assert sd.deployed_ref(repo, "deadbeef", "app") == "registry.fly.io/pb-app-web:app-deadbeef"
    # keyed by commit: another commit carries no record (so it re-derives / re-builds)
    assert sd.deployed_ref(repo, "cafebabe", "app") is None


def test_standalone_deploy_uses_the_recorded_ref_without_rebuilding(tmp_path, monkeypatch):
    """`--stage deploy` alone must release the image recorded for the current
    promoted commit -- no build, no new commit."""
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00deadbeef")
    sd.record_image_ref(tmp_path, "c2356b213f00deadbeef", "app",
                        "registry.fly.io/pb-app-web:app-c2356b213f00")
    sd.record_image_ref(tmp_path, "c2356b213f00deadbeef", "science",
                        "registry.fly.io/pb-app-worker:science-c2356b213f00")
    seen = []

    def fake_flyctl(argv, cwd, timeout):
        seen.append(argv)
        return {"ok": True, "exit_code": 0, "stdout": "Released v42\n", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake_flyctl)
    args = Namespace(fly_app=None, no_drain=True, drain_command=None)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["deploy"])
    assert rc == 0
    # deploy only: never a build-only run
    assert not any("--build-only" in a for a in seen)
    assert len(seen) == len(records) == 4
    assert any("registry.fly.io/pb-app-web:app-c2356b213f00" in a for a in seen)


# --------------------------------------------------------------------------
# Failure visibility: a failed release sets a non-zero exit status
# --------------------------------------------------------------------------

def test_a_failed_release_fails_the_command_and_prints_the_outcome(tmp_path, monkeypatch, capsys):
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path, ["web"])
    refs = {"app": "registry.fly.io/pb-app-web:app-c2356b213f00"}

    def fake_flyctl(argv, cwd, timeout):
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "Error build failed: boom"}

    monkeypatch.setattr(cli, "_flyctl", fake_flyctl)
    failed = cli._ship_deploy_apps(tmp_path, "c2356b213f00" * 2, records, refs, False, None)
    assert failed is True
    out = capsys.readouterr().out
    assert "release FAILED" in out
    assert "boom" in out  # tail of flyctl output is printed
    assert "registry.fly.io/pb-app-web:app-c2356b213f00" in out  # the image ref is printed


def test_a_failed_image_build_fails_the_command(tmp_path, monkeypatch, capsys):
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path, ["web"])

    def fake_flyctl(argv, cwd, timeout):
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "could not build"}

    monkeypatch.setattr(cli, "_flyctl", fake_flyctl)
    refs, failed = cli._ship_build_images(tmp_path, "c2356b213f00" * 2, records, False)
    assert failed is True and refs == {}
    assert "build FAILED" in capsys.readouterr().out


# --------------------------------------------------------------------------
# End-to-end dry-run rehearsal (no flyctl, no mutations)
# --------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True)
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


def test_dry_run_rehearses_both_images_and_all_four_apps(tmp_path, monkeypatch, capsys):
    _origin, repo = _repo(tmp_path)
    _write_all(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fly configs")
    _git(repo, "push", "origin", "main")
    monkeypatch.setattr(cli, "_ship_find_main_checkout", lambda base: repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    calls = []
    monkeypatch.setattr(cli, "_flyctl", lambda *a, **k: calls.append(a) or {"ok": True})
    args = Namespace(dry_run=True, fly_app=None, no_drain=False, drain_command=None, deploy=False)

    rc = cli._execute_ship_deployment(args, "main", "castle", quests=[], stages=["build", "deploy"])

    assert rc == 0
    assert calls == [], "dry-run must not execute flyctl"
    out = capsys.readouterr().out
    assert "DRY RUN PASSED" in out
    # two builds, one per image, both build-only + push
    assert out.count("flyctl deploy --build-only --push") == 2
    # four releases, all by pre-built image (never build-only)
    assert out.count("flyctl deploy --image") == 4
    for config in _APPS:
        assert f"-c {config}" in out
    # the app image ref is used for web + worker-light; the science ref for the two workers
    assert out.count("registry.fly.io/pb-app-web:app-") == 2
    assert out.count("registry.fly.io/pb-app-worker:science-") == 2
