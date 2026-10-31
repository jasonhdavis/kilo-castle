"""
Configuration management for Kilo Castle / The Court.

Q455: `.court/config.json` is the SINGLE SOURCE OF TRUTH for court-engine
role configuration:

- `models.<role>`            — the per-role model (serf, master_of_coin,
                              gatekeeper, steward, artist, scout).
- `models.<role>_provider`   — the Kilo CLI provider for a role.
- `model_aliases`            — human/display model name -> qualified
                              provider/model ID (used by canonical_model_id).
- `suite.command`            — the canonical unified integration suite
                              invocation (git_ops suite detection reads this
                              first; the manage.py/pytest probe is fallback).
- `sync.auto_interval_minutes`— dashboard (`court ui`) auto-reconcile cadence
                              for the classified `court sync --all` fold
                              (absent/0 disables; see `get_auto_sync`).
- `context.sweet_spot` / `context.cull_soft` / `context.hard_red_zone`
                             — the soft/hard token budgets the engine uses to
                              decide whether to continue a worker session or
                              cold-start a fresh one (`get_context_limits`); a
                              `context.per_role.<role>` block may override any
                              of them per Court role.

No model-ID constants live in engine code: every role model resolves through
`get_model()` / `get_provider()` against the manifest. A missing role is a
loud `CourtConfigError`, never a silent hardcoded fallback.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

# Roles that must be present in the manifest's models map. Documentation
# (.court/README.md, AGENTS.md, .court/GLOSSARY.md) and `court model <role>`
# resolve against these; adding a role means adding it here AND to
# .court/config.json — not to engine code.
KNOWN_ROLE_MODELS = ("serf", "master_of_coin", "gatekeeper", "steward", "artist", "scout")

# Structural defaults only — deliberately NO model IDs here. The manifest
# (.court/config.json) is the sole carrier of role-model values.
DEFAULT_CONFIG: dict[str, Any] = {
    "no_kilo_mode": False,
}


class CourtConfigError(RuntimeError):
    """Raised when a required role model is missing from .court/config.json."""


def find_court_dir(start: Optional[Path] = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        cand = d / ".court"
        if cand.is_dir():
            return cand
    return (start or Path.cwd()).resolve() / ".court"


def load_config(court_dir: Optional[Path] = None) -> dict[str, Any]:
    cd = court_dir or find_court_dir()
    cfg_file = cd / "config.json"
    cfg: dict[str, Any] = dict(DEFAULT_CONFIG)
    if cfg_file.is_file():
        try:
            user_data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(user_data, dict):
                if isinstance(user_data.get("models"), dict):
                    models = dict(cfg.get("models") or {})
                    models.update(user_data["models"])
                    cfg["models"] = models
                for k, v in user_data.items():
                    if k != "models":
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def get_model(role: str, default: Optional[str] = None, court_dir: Optional[Path] = None) -> str:
    """Resolve a role model from the manifest (.court/config.json models map).

    Raises CourtConfigError when the role is absent and no explicit default is
    supplied — a missing manifest entry must be fixed in the manifest, not
    papered over by an engine constant.
    """
    cfg = load_config(court_dir)
    models = cfg.get("models", {})
    value = models.get(role)
    if value is not None and str(value).strip():
        return str(value).strip()
    if default is not None:
        return default
    raise CourtConfigError(
        f"role model '{role}' is not set in .court/config.json (models.{role}) — "
        "the manifest is the single source of truth for role models; add the entry there"
    )


def get_provider(role: str, default: Optional[str] = None, court_dir: Optional[Path] = None) -> str:
    """Resolve a role's Kilo CLI provider (models.<role>_provider in the manifest)."""
    cfg = load_config(court_dir)
    models = cfg.get("models", {})
    value = models.get(f"{role}_provider")
    if value is not None and str(value).strip():
        return str(value).strip()
    return default if default is not None else "openrouter"


def get_suite_command(court_dir: Optional[Path] = None) -> str:
    """Canonical unified integration suite command from the manifest (suite.command)."""
    cfg = load_config(court_dir)
    suite = cfg.get("suite")
    if isinstance(suite, dict):
        cmd = str(suite.get("command") or "").strip()
        if cmd:
            return cmd
    return ""


def get_harness_command(court_dir: Optional[Path] = None) -> str:
    """Castle's read-only production verification harness from the manifest
    (harness.command), e.g. pb-app's ROQ harness (scripts/db/roq.py). Used by
    charter/MoC protocols for data-mutation quests: a dry-run against real
    data (exact affected-row counts, match rates) is the only acceptable
    evidence that a backfill/batch job selects and writes the intended rows."""
    cfg = load_config(court_dir)
    harness = cfg.get("harness")
    if isinstance(harness, dict):
        cmd = str(harness.get("command") or "").strip()
        if cmd:
            return cmd
    return ""


def get_auto_sync(court_dir: Optional[Path] = None) -> dict:
    """Dashboard ledger auto-reconcile from the manifest (sync.*).

    The recurring "ledger lag" class: a worktree-side ``court advance`` writes
    only that branch's own quest copy, so the main checkout's ledger keeps
    reading WORKING/TRIBUTE_READY while the branch tip already sits at GATE.
    Nothing folded that automatically, so the board overstated "in progress"
    and the gate-ready/collectable set understated, until someone ran the
    classified, branch-wins ``court sync --all`` by hand.

    ``sync.auto_interval_minutes`` lets the long-lived dashboard server
    (``court ui``) run that same classified fold on a fixed cadence. Absent or
    0 disables it; a positive value enables it and sets the period in minutes.
    Returns ``{"enabled": bool, "interval_minutes": float}``.
    """
    cfg = load_config(court_dir)
    sync = cfg.get("sync") if isinstance(cfg.get("sync"), dict) else {}
    try:
        interval = float(sync.get("auto_interval_minutes", 0) or 0)
    except (TypeError, ValueError):
        interval = 0.0
    return {"enabled": interval > 0, "interval_minutes": interval}


def get_studio_freshness(court_dir: Optional[Path] = None) -> dict:
    """Combined-studio freshness-gate configuration from the manifest
    (studio.freshness_command, studio.freshness_max_age_hours).

    The freshness gate is project-specific (e.g. pb-app:
    ``python3 scripts/db/local_db.py --age`` reporting the local production
    mirror's sync age). It is WARN-ONLY by design: a stale mirror delays the
    review, it must never auto-sync from a studio command. Returns
    ``{"command": str, "max_age_hours": float}``; an empty command means the
    gate is not configured and the studio reports that fact instead."""
    cfg = load_config(court_dir)
    studio = cfg.get("studio") if isinstance(cfg.get("studio"), dict) else {}
    try:
        max_age = float(studio.get("freshness_max_age_hours", 24) or 24)
    except (TypeError, ValueError):
        max_age = 24.0
    return {
        "command": str(studio.get("freshness_command") or "").strip(),
        "max_age_hours": max_age,
    }


# Context-window budget: the soft/hard thresholds the engine uses to decide
# whether to CONTINUE a worker session or COLD-START a fresh one. A session's
# true context (`tokens.total` on its last step-finish) is re-read in full on
# every continuation turn, so past a point a fresh session that reads the
# charter and worktree from disk is both cheaper and sharper.
#   sweet_spot     — the comfortable working size (informational).
#   cull_soft      — a cull OPPORTUNITY: prefer a fresh session once a session
#                    reaches this AND the worktree is committed (uncommitted
#                    work keeps the session so a cold start cannot lose it).
#   hard_red_zone  — the red zone: never re-prompt a same-session continuation.
DEFAULT_CONTEXT_SWEET_SPOT = 200_000
DEFAULT_CONTEXT_CULL_SOFT = 350_000
DEFAULT_CONTEXT_HARD_RED_ZONE = 750_000


def get_context_limits(role: Optional[str] = None, court_dir: Optional[Path] = None) -> dict:
    """Resolve the soft/hard context thresholds from the manifest (context.*).

    Global values come from ``context.sweet_spot`` / ``context.cull_soft`` /
    ``context.hard_red_zone``; a ``context.per_role.<role>`` block may override
    any of them per Court role. Absent, non-numeric, or negative values fall
    back to the structural defaults above. Returns a dict of ints keyed
    ``sweet_spot`` / ``cull_soft`` / ``hard_red_zone`` (0 disables a threshold).
    """
    cfg = load_config(court_dir)
    ctx = cfg.get("context") if isinstance(cfg.get("context"), dict) else {}

    def _int(source: dict, key: str, default: int) -> int:
        try:
            value = int(source.get(key, default))
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    limits = {
        "sweet_spot": _int(ctx, "sweet_spot", DEFAULT_CONTEXT_SWEET_SPOT),
        "cull_soft": _int(ctx, "cull_soft", DEFAULT_CONTEXT_CULL_SOFT),
        "hard_red_zone": _int(ctx, "hard_red_zone", DEFAULT_CONTEXT_HARD_RED_ZONE),
    }
    per = ctx.get("per_role") if isinstance(ctx.get("per_role"), dict) else {}
    override = per.get(role) if role and isinstance(per.get(role), dict) else None
    if isinstance(override, dict):
        for key in limits:
            if key in override:
                limits[key] = _int(override, key, limits[key])
    return limits


def canonical_model_id(model_str: str, provider: Optional[str] = None, court_dir: Optional[Path] = None) -> str:
    """Map human/display model names to fully qualified provider/model strings for Kilo CLI.

    Alias resolution reads the manifest's `model_aliases` block (Q455) instead
    of an engine-side hardcoded table. Qualified IDs pass through unchanged.
    """
    if not model_str:
        return ""
    m = model_str.strip()
    if "/" in m:
        return m
    low = m.lower().replace(" ", "").replace("-", "").replace(".", "")
    aliases = load_config(court_dir).get("model_aliases", {})
    if isinstance(aliases, dict):
        for name, qualified in aliases.items():
            norm = str(name).lower().replace(" ", "").replace("-", "").replace(".", "")
            if norm and norm == low and str(qualified).strip():
                return str(qualified).strip()
    p = provider or "openrouter"
    return f"{p}/{m}"
