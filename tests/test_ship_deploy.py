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
# Fleet targeting: a named fleet -> the app group it deploys
# --------------------------------------------------------------------------

def test_fleet_expands_to_its_app_group():
    assert list(sd.FLY_FLEETS) == ["web", "worker", "all"]
    assert sd.fleet_aliases("web") == ("web",)
    assert sd.fleet_aliases("worker") == ("worker", "worker-heavy", "worker-light")
    assert sd.fleet_aliases("nope") is None
    # canonical order, de-duped, independent of the order the fleets were named
    assert sd.expand_selection(fleets=["worker"]) == (["worker", "worker-heavy", "worker-light"], [])
    assert sd.expand_selection(fleets=["all"]) == (
        ["web", "worker", "worker-heavy", "worker-light"], []
    )


def test_fleet_composes_with_fly_app_as_a_union():
    aliases, errors = sd.expand_selection(fly_apps=["web"], fleets=["worker"])
    assert errors == []
    assert aliases == ["web", "worker", "worker-heavy", "worker-light"]


def test_unknown_fleet_is_refused():
    aliases, errors = sd.expand_selection(fleets=["nope"])
    assert aliases == []
    assert any("unknown --fleet" in e for e in errors)


def test_fleet_scopes_the_deploy_plan(tmp_path):
    _write_all(tmp_path)
    args = Namespace(fly_app=None, fleet=["web"])
    records, errors = cli._ship_resolve_deploy_plan(args, tmp_path)
    assert errors == []
    assert [r["app"] for r in records] == ["pb-app-web"]
    # a web-only fleet needs only the app image, so the science image is not built
    assert sd.images_for_records(records) == ["app"]


def test_worker_fleet_builds_both_images_and_releases_all_workers(tmp_path):
    _write_all(tmp_path)
    args = Namespace(fly_app=None, fleet=["worker"])
    records, errors = cli._ship_resolve_deploy_plan(args, tmp_path)
    assert errors == []
    assert [r["app"] for r in records] == [
        "pb-app-worker", "pb-app-worker-heavy", "pb-app-worker-light"
    ]
    assert sd.images_for_records(records) == ["app", "science"]


def test_fleet_targets_only_that_fleet_for_build_and_release(tmp_path, monkeypatch):
    """`--fleet web` builds only the app image and releases only pb-app-web."""
    _write_all(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00")
    seen = []

    def fake_flyctl(argv, cwd, timeout):
        seen.append(argv)
        return {"ok": True, "exit_code": 0,
                "stdout": "registry.fly.io/pb-app-web:app-c2356b213f00 pushed\nReleased v7\n",
                "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake_flyctl)
    args = Namespace(fly_app=None, fleet=["web"], no_drain=True, drain_command=None, dry_run=False)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["build", "deploy"])
    assert rc == 0
    # one build (the app image only), then one release (pb-app-web only)
    assert sum(1 for a in seen if "--build-only" in a) == 1
    deploys = [a for a in seen if "--image" in a]
    assert len(deploys) == 1
    assert "fly.toml" in deploys[0]  # the web app's config
    assert not any("fly-worker" in " ".join(a) for a in deploys)


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
    # --pty=false is pinned: `flyctl ssh console` must not need a TTY.
    assert sd.drain_argv("pb-app-worker", "drain_worker_fleet") == [
        "flyctl", "ssh", "console", "-a", "pb-app-worker",
        "--pty=false", "-C", "python manage.py drain_worker_fleet",
    ]


def test_resume_argv_runs_the_counterpart_command():
    assert sd.resume_argv("pb-app-worker") == [
        "flyctl", "ssh", "console", "-a", "pb-app-worker",
        "--pty=false", "-C", "python manage.py resume_worker_fleet",
    ]


def test_machine_list_argv_asks_for_json():
    assert sd.machine_list_argv("pb-app-worker-light") == [
        "flyctl", "machine", "list", "-a", "pb-app-worker-light", "--json",
    ]


def test_running_machine_count_reads_flyctl_json():
    assert sd.running_machine_count('[{"id": "a", "state": "started"}, {"id": "b", "state": "stopped"}]') == 1
    assert sd.running_machine_count('[{"id": "a", "state": "suspended"}]') == 0
    assert sd.running_machine_count('[]') == 0
    # unusable payloads must not read as "no running machine"
    assert sd.running_machine_count('') is None
    assert sd.running_machine_count('not json') is None
    assert sd.running_machine_count('{"id": "a"}') is None


def test_namespace_members_are_apps_whose_namespace_differs_from_the_build(tmp_path):
    _write_all(tmp_path)
    # the app image builds in pb-app-web's namespace, so only worker-light needs a copy
    assert sd.namespace_members("app", tmp_path) == ("pb-app-worker-light",)
    # the science image builds in pb-app-worker's namespace; heavy needs a copy
    assert sd.namespace_members("science", tmp_path) == ("pb-app-worker-heavy",)


def test_image_ref_can_select_a_member_namespace(tmp_path):
    _write_all(tmp_path)
    assert sd.image_ref("app", tmp_path, "app-abc") == "registry.fly.io/pb-app-web:app-abc"
    assert sd.image_ref("app", tmp_path, "app-abc", app_name="pb-app-worker-light") == (
        "registry.fly.io/pb-app-worker-light:app-abc"
    )


def test_docker_login_argv_is_org_wide_and_takes_no_app():
    """`fly auth docker` takes no app: `-a` is not a flag it accepts on flyctl
    v0.5.3 ("unknown shorthand flag: 'a' in -a")."""
    assert sd.docker_login_argv() == ["flyctl", "auth", "docker"]
    assert "-a" not in sd.docker_login_argv()
    assert "--app" not in sd.docker_login_argv()


def test_docker_tag_and_push_argv():
    assert sd.docker_tag_argv("registry.fly.io/pb-app-web:T", "registry.fly.io/pb-app-worker-light:T") == [
        "docker", "tag", "registry.fly.io/pb-app-web:T", "registry.fly.io/pb-app-worker-light:T",
    ]
    assert sd.docker_push_argv("registry.fly.io/pb-app-worker-light:T") == [
        "docker", "push", "registry.fly.io/pb-app-worker-light:T",
    ]


def test_looks_like_registry_auth_error():
    assert sd.looks_like_registry_auth_error("unauthorized: authentication required")
    assert sd.looks_like_registry_auth_error("no basic auth credentials")
    assert sd.looks_like_registry_auth_error("Error response from daemon: 401 Unauthorized")
    assert not sd.looks_like_registry_auth_error("manifest unknown")
    assert not sd.looks_like_registry_auth_error("")


def test_namespace_members_can_be_scoped_to_the_deployed_apps(tmp_path):
    _write_all(tmp_path)
    # unscoped: every member of the image except the build app
    assert sd.namespace_members("app", tmp_path) == ("pb-app-worker-light",)
    # scoped to a release that does not include worker-light -> nothing to fan out
    assert sd.namespace_members("app", tmp_path, apps=["pb-app-web", "pb-app-worker"]) == ()
    # scoped to a release that does include it -> exactly that member
    assert sd.namespace_members(
        "app", tmp_path, apps=["pb-app-web", "pb-app-worker-light"]) == ("pb-app-worker-light",)
    # the science image, scoped to a release without worker-heavy
    assert sd.namespace_members("science", tmp_path, apps=["pb-app-web", "pb-app-worker"]) == ()


def test_auth_runs_immediately_before_each_push_and_has_no_app_flag(tmp_path, monkeypatch):
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(list(argv))
        return {"ok": True, "exit_code": 0, "stdout": "Authentication successful.", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    ok = cli._ship_replicate_image(
        tmp_path, "registry.fly.io/pb-app-web:app-x",
        "registry.fly.io/pb-app-worker-light:app-x", "pb-app-worker-light",
    )
    assert ok is True
    assert calls[0] == ["docker", "info"], "liveness is probed before any push"
    assert calls[1:] == [
        ["flyctl", "auth", "docker"],
        ["docker", "tag", "registry.fly.io/pb-app-web:app-x", "registry.fly.io/pb-app-worker-light:app-x"],
        ["docker", "push", "registry.fly.io/pb-app-worker-light:app-x"],
    ]


def test_an_expired_credential_is_re_authed_and_the_push_retried(tmp_path, monkeypatch, capsys):
    """The credential expires in ~9 minutes; a push rejected as unauthorized is
    re-authenticated and retried once rather than failing the deploy."""
    calls = []
    pushes = {"n": 0}

    def fake(argv, cwd, timeout):
        calls.append(list(argv))
        if argv[:3] == ["flyctl", "auth", "docker"]:
            return {"ok": True, "exit_code": 0, "stdout": "Authentication successful.", "stderr": ""}
        if argv[:2] == ["docker", "push"]:
            pushes["n"] += 1
            if pushes["n"] == 1:
                return {"ok": False, "exit_code": 1, "stdout": "",
                        "stderr": "unauthorized: authentication required"}
            return {"ok": True, "exit_code": 0, "stdout": "pushed", "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    ok = cli._ship_replicate_image(tmp_path, "src", "dst", "pb-app-worker-light")
    assert ok is True
    assert sum(1 for a in calls if a[:3] == ["flyctl", "auth", "docker"]) == 2
    assert sum(1 for a in calls if a[:2] == ["docker", "push"]) == 2


def test_a_dead_docker_daemon_falls_back_instead_of_failing_the_deploy(tmp_path, monkeypatch, capsys):
    """The docker CLI can be on PATH with no daemon answering (Docker Desktop
    stopped): the fan-out must warn and report "not copied" (so the caller
    releases the app from the shared ref) rather than withholding an
    otherwise-fine release."""
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(list(argv))
        if argv[:2] == ["docker", "info"]:
            return {"ok": False, "exit_code": 1, "stdout": "",
                    "stderr": "Cannot connect to the Docker daemon at unix:///... Is the docker daemon running?"}
        raise AssertionError(f"no registry command may run once the daemon is dead: {argv}")

    monkeypatch.setattr(cli, "_flyctl", fake)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/docker")
    copied = cli._ship_replicate_image(tmp_path, "src", "dst", "pb-app-worker-light")
    assert copied is False, "a skipped copy must report False so the caller falls back to the shared ref"
    out = capsys.readouterr().out
    assert "docker is unavailable" in out and "cross-namespace" in out
    assert calls == [["docker", "info"]]


def test_the_fan_out_is_scoped_to_the_deployed_apps(tmp_path, monkeypatch):
    """`--fly-app web,worker,worker-heavy` must not tag or push into
    pb-app-worker-light's namespace: that app is not part of this release."""
    _write_all(tmp_path)
    records, errors = sd.resolve_apps(tmp_path, ["web", "worker", "worker-heavy"])
    assert errors == []
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(" ".join(argv))
        return {"ok": True, "exit_code": 0,
                "stdout": "registry.fly.io/pb-app-worker-heavy:science-c2356b213f00 pushed", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    _refs, failed = cli._ship_build_images(tmp_path, "c2356b213f00", records, False)
    assert failed is False
    assert not any("pb-app-worker-light" in c for c in calls), "an out-of-release app must not be fanned out"
    assert any("pb-app-worker-heavy" in c for c in calls)


def test_per_app_ref_prefers_its_own_namespace_then_falls_back_to_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    repo = tmp_path / "repo"
    repo.mkdir()
    sd.record_image_ref(repo, "sha1", "app", "registry.fly.io/pb-app-web:app-sha1")
    # no per-app copy yet -> the shared ref is used
    assert sd.deployed_ref(repo, "sha1", "app", app_name="pb-app-worker-light") == (
        "registry.fly.io/pb-app-web:app-sha1"
    )
    sd.record_image_ref(repo, "sha1", "app", "registry.fly.io/pb-app-worker-light:app-sha1",
                        app_name="pb-app-worker-light")
    # once replicated, the app's own namespace wins
    assert sd.deployed_ref(repo, "sha1", "app", app_name="pb-app-worker-light") == (
        "registry.fly.io/pb-app-worker-light:app-sha1"
    )
    # the shared ref is still intact for the build-namespace app
    assert sd.deployed_ref(repo, "sha1", "app") == "registry.fly.io/pb-app-web:app-sha1"


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
# Drain invocation: no-running-machine, absent, running, failed
# --------------------------------------------------------------------------

def _drain_hook(tmp_path):
    cmd = tmp_path / "apps" / "tasks" / "management" / "commands" / "drain_worker_fleet.py"
    cmd.parent.mkdir(parents=True, exist_ok=True)
    cmd.write_text("from django.core.management.base import BaseCommand\n", encoding="utf-8")
    return cmd


def _drain_record(app="pb-app-worker"):
    return {"app": app, "role": "worker"}


def test_drain_absent_hook_logs_and_continues(tmp_path, capsys):
    assert cli._ship_pre_deploy_drain(tmp_path, _drain_record(), "drain_worker_fleet", False) == "absent"
    assert "not present" in capsys.readouterr().out


def test_drain_skips_an_app_with_no_running_machine(tmp_path, monkeypatch, capsys):
    """An app scaled to zero (light) or suspended (heavy) cannot be ssh'd into and
    has nothing in flight: that reads as already-drained, never as a failure."""
    _drain_hook(tmp_path)
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(argv)
        return {"ok": True, "exit_code": 0,
                "stdout": '[{"id": "a", "state": "stopped"}, {"id": "b", "state": "suspended"}]',
                "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    assert cli._ship_pre_deploy_drain(tmp_path, _drain_record(), "drain_worker_fleet", False) == "absent"
    out = capsys.readouterr().out
    assert "nothing can be in flight" in out
    assert all("ssh" not in a for a in calls), "must not attempt the drain with no machine"


def test_drain_runs_when_a_machine_is_started(tmp_path, monkeypatch, capsys):
    _drain_hook(tmp_path)
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(argv)
        if "machine" in argv:
            return {"ok": True, "exit_code": 0, "stdout": '[{"id": "a", "state": "started"}]', "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "DRAIN COMPLETE", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    assert cli._ship_pre_deploy_drain(tmp_path, _drain_record(), "drain_worker_fleet", False) == "ok"
    ssh = [a for a in calls if "ssh" in a]
    assert ssh and ssh[0] == sd.drain_argv("pb-app-worker", "drain_worker_fleet")


def test_drain_failure_withholds_the_release(tmp_path, monkeypatch, capsys):
    _drain_hook(tmp_path)

    def fake(argv, cwd, timeout):
        if "machine" in argv:
            return {"ok": True, "exit_code": 0, "stdout": '[{"id": "a", "state": "started"}]', "stderr": ""}
        return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "DRAIN TIMEOUT: 3 leases in flight"}

    monkeypatch.setattr(cli, "_flyctl", fake)
    assert cli._ship_pre_deploy_drain(tmp_path, _drain_record(), "drain_worker_fleet", False) == "failed"
    out = capsys.readouterr().out
    assert "release withheld" in out and "DRAIN TIMEOUT" in out


def test_no_running_machine_does_not_withhold_the_release_end_to_end(tmp_path, monkeypatch, capsys):
    """The whole deploy path: a worker app with no running machine still releases."""
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path, ["worker-light"])
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    released = []

    def fake(argv, cwd, timeout):
        if "machine" in argv:
            return {"ok": True, "exit_code": 0, "stdout": '[]', "stderr": ""}
        if "deploy" in argv:
            released.append(argv)
            return {"ok": True, "exit_code": 0, "stdout": "Released v7", "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    refs = {"app": "registry.fly.io/pb-app-web:app-sha"}
    failed = cli._ship_deploy_apps(tmp_path, "sha", records, refs, False, "drain_worker_fleet")
    assert failed is False
    assert released, "the release must still run"
    assert "--image" in released[0]


# --------------------------------------------------------------------------
# Blind `--stage deploy` is refused
# --------------------------------------------------------------------------

def test_deploy_apps_refuses_without_a_recorded_build(tmp_path, monkeypatch, capsys):
    _write_all(tmp_path)
    records, _ = sd.resolve_apps(tmp_path, ["web"])
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = []
    monkeypatch.setattr(cli, "_flyctl", lambda *a, **k: called.append(a) or {"ok": True})
    failed = cli._ship_deploy_apps(tmp_path, "c2356b213f00" * 2, records, {}, False, None)
    assert failed is True
    assert called == [], "a derived reference must never be released"
    assert "no built image recorded" in capsys.readouterr().out


def test_standalone_deploy_stage_refuses_with_no_build_recorded(tmp_path, monkeypatch, capsys):
    """`--stage deploy` alone needs a recorded build for the promoted commit."""
    _write_all(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00deadbeef")
    args = Namespace(fly_app=None, no_drain=True, drain_command=None)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["deploy"])
    assert rc == 1
    assert "no built image recorded" in capsys.readouterr().out


def test_standalone_deploy_stage_runs_with_a_recorded_build(tmp_path, monkeypatch):
    _write_all(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    sd.record_image_ref(tmp_path, "c2356b213f00deadbeef", "app",
                        "registry.fly.io/pb-app-web:app-c2356b213f00")
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00deadbeef")
    seen = []

    def fake(argv, cwd, timeout):
        if "deploy" in argv:
            seen.append(argv)
        return {"ok": True, "exit_code": 0, "stdout": "Released v3", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    args = Namespace(fly_app=["web"], no_drain=True, drain_command=None)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["deploy"])
    assert rc == 0
    assert len(seen) == 1
    assert "registry.fly.io/pb-app-web:app-c2356b213f00" in seen[0]


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
# Namespace replication is OPTIONAL: a skipped copy must never fail the run
# --------------------------------------------------------------------------

def test_without_digest_strips_only_a_trailing_manifest_digest():
    digest = "@sha256:" + "a" * 64
    assert sd.without_digest("registry.fly.io/pb-app-web:app-x" + digest) == (
        "registry.fly.io/pb-app-web:app-x"
    )
    assert sd.without_digest("registry.fly.io/pb-app-web:app-x") == (
        "registry.fly.io/pb-app-web:app-x"
    )
    assert sd.without_digest("") == ""
    assert sd.source_ref_candidates("registry.fly.io/pb-app-web:app-x" + digest) == [
        "registry.fly.io/pb-app-web:app-x", "registry.fly.io/pb-app-web:app-x" + digest,
    ]
    # a digest-free ref is already the local tag form: a single candidate, no probe
    assert sd.source_ref_candidates("registry.fly.io/pb-app-web:app-x") == [
        "registry.fly.io/pb-app-web:app-x"
    ]
    assert sd.docker_inspect_argv("registry.fly.io/pb-app-web:app-x") == [
        "docker", "image", "inspect", "registry.fly.io/pb-app-web:app-x"
    ]


def test_tag_uses_the_digest_free_local_ref(tmp_path, monkeypatch):
    """flyctl reports the pushed image with a `@sha256:` manifest digest, which the
    local daemon does not know by that name; the tag must copy from the digest-free
    tag the daemon actually holds (the root cause of the 2026-11-14 false-FAIL)."""
    digest = "@sha256:" + "c" * 64
    source = "registry.fly.io/pb-app-web:app-x" + digest
    calls = []

    def fake(argv, cwd, timeout):
        calls.append(list(argv))
        if argv[:2] == ["docker", "info"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:3] == ["flyctl", "auth", "docker"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:3] == ["docker", "image", "inspect"]:
            return {"ok": argv[3] == "registry.fly.io/pb-app-web:app-x",
                    "exit_code": 0, "stdout": "[]", "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "pushed", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    copied = cli._ship_replicate_image(
        tmp_path, source, "registry.fly.io/pb-app-worker-light:app-x", "pb-app-worker-light")
    assert copied is True
    tag = [c for c in calls if c[:2] == ["docker", "tag"]]
    assert tag and tag[0][2] == "registry.fly.io/pb-app-web:app-x", (
        "docker tag must copy from the digest-free local ref, not the manifest ref"
    )


def test_replication_failure_does_not_fail_a_successful_release(tmp_path, monkeypatch, capsys):
    """The key regression: tag/replicate fails, EVERY release succeeds -> exit 0
    and the driver says why the copy was skipped (no contradicting ❌)."""
    _write_all(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00")
    digest = "@sha256:" + "d" * 64

    def fake(argv, cwd, timeout):
        if argv[:2] == ["docker", "info"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:3] == ["flyctl", "auth", "docker"]:
            return {"ok": True, "exit_code": 0, "stdout": "auth ok", "stderr": ""}
        if argv[:3] == ["docker", "image", "inspect"]:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "No such image"}
        if argv[:2] == ["docker", "tag"]:
            return {"ok": False, "exit_code": 1, "stdout": "",
                    "stderr": f"Error response from daemon: No such image: {argv[2]}"}
        if "--build-only" in argv:
            app = "pb-app-web" if "fly.toml" in argv else "pb-app-worker"
            label = argv[argv.index("--image-label") + 1]
            return {"ok": True, "exit_code": 0,
                    "stdout": f"registry.fly.io/{app}:{label}{digest} pushed", "stderr": ""}
        if "--image" in argv:
            return {"ok": True, "exit_code": 0, "stdout": "Released v100\n", "stderr": ""}
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(cli, "_flyctl", fake)
    args = Namespace(fly_app=None, fleet=None, no_drain=True, drain_command=None, dry_run=False)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["build", "deploy"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "replication into 'pb-app-worker-light' skipped" in out
    assert "released from the shared build namespace" in out
    assert "release FAILED" not in out


def test_a_genuine_release_failure_still_exits_nonzero(tmp_path, monkeypatch, capsys):
    """The non-zero contract for a real failure is unchanged by the optional-copy fix."""
    _write_all(tmp_path)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_ship_promoted_sha", lambda wt, base: "c2356b213f00")

    def fake(argv, cwd, timeout):
        if argv[:2] == ["docker", "info"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:3] == ["flyctl", "auth", "docker"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:2] == ["docker", "tag"]:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "No such image"}
        if "--build-only" in argv:
            return {"ok": True, "exit_code": 0,
                    "stdout": "registry.fly.io/pb-app-web:app-x pushed", "stderr": ""}
        if "--image" in argv:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "boom"}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    args = Namespace(fly_app=None, fleet=None, no_drain=True, drain_command=None, dry_run=False)
    rc = cli._ship_deploy_stages(args, tmp_path, "main", ["build", "deploy"])
    assert rc == 1
    assert "release FAILED" in capsys.readouterr().out


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


def test_tag_failure_with_all_releases_succeeding_reports_released(tmp_path, monkeypatch, capsys):
    """End-to-end verdict (the 2026-11-14 false-FAIL): replication is skipped but
    all four apps release, so the final line says RELEASED, not FAILED, and the
    command exits 0."""
    _origin, repo = _repo(tmp_path)
    _write_all(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "fly configs")
    _git(repo, "push", "origin", "main")
    monkeypatch.setattr(cli, "_ship_find_main_checkout", lambda base: repo)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    digest = "@sha256:" + "e" * 64

    def fake(argv, cwd, timeout):
        if argv[:2] == ["docker", "info"]:
            return {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
        if argv[:3] == ["flyctl", "auth", "docker"]:
            return {"ok": True, "exit_code": 0, "stdout": "auth ok", "stderr": ""}
        if argv[:3] == ["docker", "image", "inspect"]:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "No such image"}
        if argv[:2] == ["docker", "tag"]:
            return {"ok": False, "exit_code": 1, "stdout": "", "stderr": "No such image"}
        if "--build-only" in argv:
            app = "pb-app-web" if "fly.toml" in argv else "pb-app-worker"
            label = argv[argv.index("--image-label") + 1]
            return {"ok": True, "exit_code": 0,
                    "stdout": f"registry.fly.io/{app}:{label}{digest} pushed", "stderr": ""}
        if "--image" in argv:
            return {"ok": True, "exit_code": 0, "stdout": "Released v7\n", "stderr": ""}
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cli, "_flyctl", fake)
    args = Namespace(dry_run=False, fly_app=None, fleet=None, no_drain=True,
                     drain_command=None, deploy=True)
    rc = cli._execute_ship_deployment(args, "main", "castle", quests=[], stages=["build", "deploy"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "released to Fly" in out
    assert "Fly build/deploy FAILED" not in out
