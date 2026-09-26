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
