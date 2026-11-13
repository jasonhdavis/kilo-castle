"""Court-driven Fly deploy for ``court ship --confirm --deploy``.

Why this exists
---------------
Production deploys used to happen because Fly's GitHub integration
(``fly-io[bot]``) watched ``origin/main`` and fired on a push. On
2026-11-13T00:47Z that integration created three production deployments for one
pushed commit and marked all three failed roughly twenty seconds later, before
any build stage, with no reason exposed and an auth-gated log. No Fly release
was created, so production silently kept running the previous release -- and
because ``main == origin/main`` after the push, the deploy could not be
re-triggered without a new commit. M'Lord's ruling (2026-11-13): the Court
drives the deploy itself through ``flyctl`` and GitHub leaves the deploy path.

What this module is
-------------------
Pure and stdlib-only. It resolves the app/image manifest, computes deterministic
image labels and Fly registry references, and builds the exact ``flyctl`` argv
for the ``build``/``deploy`` stages and for the pre-deploy worker drain hook. It
also owns the durable image-reference record that lets ``--stage deploy`` re-run
a release for the current promoted commit with no new commit.

It never runs ``flyctl`` -- execution lives in :mod:`court.cli`. The argv
builders and resolvers here are the unit-testable surface.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Optional

# flyctl app alias -> config file at the repo root of the production checkout.
FLY_CONFIGS = {
    "web": "fly.toml",
    "worker": "fly-worker.toml",
    "worker-heavy": "fly-worker-heavy.toml",
    "worker-light": "fly-worker-light.toml",
}

# The image manifest. Each distinct image is built ONCE and released to every
# app that shares it. App membership is explicit -- the image an app runs is
# never inferred from its name or flavor -- and each app's own fly config is
# cross-checked against it, so the mapping is derivable from the config and
# survives the planned image consolidation (Q1487) unchanged: pre-consolidation
# the light worker builds from ``Dockerfile.worker-light``, post-consolidation
# from ``Dockerfile.web``, and either way it is the *app* image -- never the
# science image.
IMAGE_MANIFEST = {
    "app": {
        "apps": ("pb-app-web", "pb-app-worker-light"),
        "build_config": "fly.toml",
        "dockerfile": "Dockerfile.web",
        "accepts_dockerfiles": ("Dockerfile.web", "Dockerfile.worker-light"),
    },
    "science": {
        "apps": ("pb-app-worker", "pb-app-worker-heavy"),
        "build_config": "fly-worker.toml",
        "dockerfile": "Dockerfile.worker",
        "accepts_dockerfiles": ("Dockerfile.worker",),
    },
}

# The one app that must never be released the science image (M'Lord, 2026-11-13).
SCIENCE_FORBIDDEN_APPS = ("pb-app-worker-light",)

# The pre-deploy drain is a Django management command. The implementation is
# delivered by a separate Quest; until it lands, the hook is absent and the
# driver logs that clearly and continues (fail-open, never fail-silent).
DEFAULT_DRAIN_COMMAND = "drain_worker_fleet"

FLY_REGISTRY = "registry.fly.io"


class DeployError(RuntimeError):
    """A refusal that must abort the deploy (never a silent continue)."""


# --------------------------------------------------------------------------
# Fly config parsing (stdlib-only line scanner; the engine carries no toml dep)
# --------------------------------------------------------------------------

def read_fly_config(cfg_path: Path) -> dict:
    """Parse the handful of fields the deploy needs out of a Fly toml:
    top-level ``app``, ``[build] dockerfile``, ``[deploy] release_command`` and
    the ``DJANGO_SETTINGS_MODULE`` from ``[env]``."""
    cfg = {"app": None, "dockerfile": None, "release_command": None, "settings_module": None}
    try:
        text = Path(cfg_path).read_text(encoding="utf-8")
    except OSError:
        return cfg
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]").strip()
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#", 1)[0].strip().strip("'\"")
        if not value:
            continue
        if section == "" and key == "app" and cfg["app"] is None:
            cfg["app"] = value
        elif section == "build" and key == "dockerfile":
            cfg["dockerfile"] = value
        elif section == "deploy" and key == "release_command":
            cfg["release_command"] = value
        elif section == "env" and key == "DJANGO_SETTINGS_MODULE":
            cfg["settings_module"] = value
    return cfg


def app_role(cfg: dict) -> str:
    """Container role, read off the app's own config: a worker app runs the
    worker settings module and needs the pre-deploy drain; a web app does not."""
    return "worker" if "worker" in (cfg.get("settings_module") or "").lower() else "web"


# --------------------------------------------------------------------------
# Image manifest resolution
# --------------------------------------------------------------------------

def image_ids() -> tuple:
    return tuple(IMAGE_MANIFEST)


def apps_for_image(image_id: str) -> tuple:
    return tuple(IMAGE_MANIFEST[image_id]["apps"])


def build_config_for_image(image_id: str) -> str:
    return IMAGE_MANIFEST[image_id]["build_config"]


def image_dockerfile(image_id: str) -> str:
    return IMAGE_MANIFEST[image_id]["dockerfile"]


def image_for_app(app_name: str) -> Optional[str]:
    """Explicit lookup: the image an app runs, never inferred from its name."""
    for image_id, spec in IMAGE_MANIFEST.items():
        if app_name in spec["apps"]:
            return image_id
    return None


def image_for_dockerfile(dockerfile: Optional[str]) -> Optional[str]:
    """The image a ``[build] dockerfile`` value belongs to, derived from the
    app's own config so the routing tracks the planned consolidation."""
    if not dockerfile:
        return None
    for image_id, spec in IMAGE_MANIFEST.items():
        if dockerfile in spec["accepts_dockerfiles"]:
            return image_id
    return None


def guard_image_delivery(app_name: str, image_id: str) -> None:
    """Hard guard: the science image must never reach pb-app-worker-light.

    Called immediately before every ``flyctl deploy`` release, so no code path
    -- a bad selection, a manifest edit, a resolution bug -- can hand that app
    the science image."""
    if app_name in SCIENCE_FORBIDDEN_APPS and image_id != "app":
        raise DeployError(
            f"refusing to release the {image_id!r} image to {app_name!r}: "
            f"{app_name!r} may only run the 'app' image"
        )


def resolve_apps(repo_root: Path, selection: Optional[list] = None) -> tuple:
    """Resolve the requested ``--fly-app`` selection to deployable app records.

    Each record: ``{alias, app, config, path, image, role, dockerfile}``. The
    image is an explicit manifest lookup, cross-checked against the app's own
    fly config ``[build] dockerfile``; a config that names a *different* known
    image is a hard error (fail closed), but an unrecognised dockerfile is
    tolerated so a cosmetic config edit cannot wedge the deploy.

    Returns ``(records, errors)``.
    """
    errors: list[str] = []
    records: list[dict] = []
    aliases = list(selection) if selection else list(FLY_CONFIGS)
    for alias in aliases:
        cfg_name = FLY_CONFIGS.get(alias)
        if not cfg_name:
            errors.append(f"unknown --fly-app {alias!r} (valid: {', '.join(FLY_CONFIGS)})")
            continue
        cfg_path = Path(repo_root) / cfg_name
        if not cfg_path.exists():
            errors.append(f"config not found: {cfg_path}")
            continue
        cfg = read_fly_config(cfg_path)
        app_name = cfg.get("app") or cfg_name
        image = image_for_app(app_name)                       # explicit, never inferred
        derived = image_for_dockerfile(cfg.get("dockerfile"))  # from the app's own fly config
        if not image:
            errors.append(
                f"{cfg_name}: app {app_name!r} is not in the image manifest "
                f"(known images: "
                + " | ".join(f"{i}={', '.join(s['apps'])}" for i, s in IMAGE_MANIFEST.items())
                + ")"
            )
            continue
        if derived and derived != image:
            errors.append(
                f"{cfg_name}: {app_name!r} maps to image {image!r} by the manifest but its "
                f"[build] dockerfile {cfg.get('dockerfile')!r} belongs to the {derived!r} image"
            )
            continue
        try:
            guard_image_delivery(app_name, image)
        except DeployError as exc:
            errors.append(f"{cfg_name}: {exc}")
            continue
        records.append({
            "alias": alias,
            "app": app_name,
            "config": cfg_name,
            "path": cfg_path,
            "image": image,
            "dockerfile": cfg.get("dockerfile"),
            "role": app_role(cfg),
            "release_command": cfg.get("release_command"),
        })
    return records, errors


def images_for_records(records: list) -> list:
    """The distinct images the given apps need, in manifest order."""
    needed = {rec["image"] for rec in records}
    return [image_id for image_id in IMAGE_MANIFEST if image_id in needed]


def registry_app_for_image(image_id: str, repo_root: Path) -> str:
    """The Fly app whose registry an image is pushed to (its build config's app)."""
    cfg = read_fly_config(Path(repo_root) / build_config_for_image(image_id))
    return cfg.get("app") or ""


def image_label(promoted_sha: str, image_id: str) -> str:
    """Deterministic image label derived from the promoted commit SHA, so the
    same commit always yields the same label and a deploy can be re-run for it."""
    return f"{image_id}-{(promoted_sha or 'unknown')[:12]}"


def image_ref(image_id: str, repo_root: Path, label: str) -> str:
    """The Fly registry reference an image built for ``image_id`` is pushed to."""
    return f"{FLY_REGISTRY}/{registry_app_for_image(image_id, repo_root)}:{label}"


_IMAGE_REF_RE = re.compile(r"registry\.fly\.io/[^\s'\"]+:[^\s'\"]+")
_RELEASE_RE = re.compile(r"\bv(\d+)\b")


def parse_image_ref(output: str, fallback: str) -> str:
    """Capture the pushed image reference flyctl prints; fall back to the
    deterministic ref when the output does not carry one."""
    match = _IMAGE_REF_RE.search(output or "")
    return match.group(0) if match else fallback


def parse_release_version(output: str) -> Optional[str]:
    """The Fly release version (``vNNN``) from flyctl output, if present."""
    matches = _RELEASE_RE.findall(output or "")
    return f"v{matches[-1]}" if matches else None


# --------------------------------------------------------------------------
# flyctl argv builders (pure: the exact command each stage would run)
# --------------------------------------------------------------------------

def build_argv(image_id: str, build_config: str, label: str) -> list:
    """Build one image in build-only mode with push, labelled by the promoted
    commit -- used for every shared app, so each image is built once."""
    return [
        "flyctl", "deploy",
        "--build-only", "--push",
        "--image-label", label,
        "-c", build_config,
        "--yes",
    ]


def deploy_argv(app_config: str, image_reference: str) -> list:
    """Release a pre-built image to one app, using that app's own config for its
    ``[deploy]`` block (release_command, env, strategy) -- no rebuild."""
    return [
        "flyctl", "deploy",
        "--image", image_reference,
        "-c", app_config,
        "--yes",
    ]


def drain_argv(app_name: str, drain_command: str) -> list:
    """Invoke the pre-deploy drain management command inside the app's fleet."""
    return [
        "flyctl", "ssh", "console",
        "-a", app_name,
        "-C", f"python manage.py {drain_command}",
    ]


def find_drain_command(repo_root: Path, name: str = DEFAULT_DRAIN_COMMAND) -> Optional[Path]:
    """Locate the drain management command source, or None when the hook has not
    been delivered yet (the fail-open case)."""
    root = Path(repo_root)
    if not root.is_dir():
        return None
    for candidate in sorted(root.rglob(f"management/commands/{name}.py")):
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------
# Durable image-reference record (lets `--stage deploy` re-run with no commit)
# --------------------------------------------------------------------------

STATE_VERSION = 1


def deploy_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "kilo-castle" / "ship-deploy"
    return Path.home() / ".local" / "state" / "kilo-castle" / "ship-deploy"


def _repo_slug(repo_root: Path) -> str:
    resolved = Path(repo_root).resolve()
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:6]
    return f"{resolved.name or 'repo'}-{digest}"


def deploy_state_path(repo_root: Path) -> Path:
    return deploy_state_dir() / f"{_repo_slug(repo_root)}.json"


def load_image_refs(repo_root: Path) -> dict:
    """``{promoted_sha: {image_id: image_ref}}`` written by the build stage."""
    try:
        payload = json.loads(deploy_state_path(repo_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(payload, dict) and isinstance(payload.get("images"), dict):
        return payload["images"]
    return {}


def record_image_ref(repo_root: Path, promoted_sha: str, image_id: str, reference: str) -> None:
    path = deploy_state_path(repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    payload["version"] = STATE_VERSION
    images = payload.setdefault("images", {})
    images.setdefault(promoted_sha, {})[image_id] = reference
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def deployed_ref(repo_root: Path, promoted_sha: str, image_id: str) -> Optional[str]:
    """The recorded reference for a previously built image, or None."""
    return load_image_refs(repo_root).get(promoted_sha, {}).get(image_id)
