"""
Configuration management for Kilo Castle / The Court.
Loads settings from .court/config.json with sensible defaults.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_CONFIG: dict[str, Any] = {
    "models": {
        "serf": "GLM-5.3-Flash",
        "serf_provider": "openrouter",
        "scout": "openrouter/z-ai/glm-5.3-flash",
        "master_of_coin": "openrouter/google/gemini-3.8-flash",
        "gatekeeper": "openrouter/google/gemini-3.8-flash",
        "steward": "openrouter/google/gemini-3.7-flash",
        "artist": "openrouter/z-ai/glm-5.3",
    },
    "no_kilo_mode": False,
}


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
    cfg = {
        "models": dict(DEFAULT_CONFIG["models"]),
        "no_kilo_mode": DEFAULT_CONFIG["no_kilo_mode"],
    }
    if cfg_file.is_file():
        try:
            user_data = json.loads(cfg_file.read_text(encoding="utf-8"))
            if isinstance(user_data, dict):
                if "models" in user_data and isinstance(user_data["models"], dict):
                    cfg["models"].update(user_data["models"])
                for k, v in user_data.items():
                    if k != "models":
                        cfg[k] = v
        except Exception:
            pass
    return cfg


def get_model(role: str, default: Optional[str] = None, court_dir: Optional[Path] = None) -> str:
    cfg = load_config(court_dir)
    models = cfg.get("models", {})
    return models.get(role, default or DEFAULT_CONFIG["models"].get(role, ""))


def canonical_model_id(model_str: str, provider: Optional[str] = None) -> str:
    """Map human/display model names to fully qualified provider/model strings for Kilo CLI."""
    if not model_str:
        return "openrouter/z-ai/glm-5.3-flash"
    m = model_str.strip()
    if "/" in m:
        return m
    low = m.lower().replace(" ", "").replace("-", "").replace(".", "")
    if "glm53flash" in low:
        return "openrouter/z-ai/glm-5.3-flash"
    if "glm53" in low:
        return "openrouter/z-ai/glm-5.3"
    if "gemini38flash" in low:
        return "openrouter/google/gemini-3.8-flash"
    if "gemini37flash" in low:
        return "openrouter/google/gemini-3.7-flash"
    p = provider or "openrouter"
    return f"{p}/{m}"
