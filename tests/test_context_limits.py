"""Tests for manifest-driven context limits (court.config.get_context_limits)."""
import json

from court import config


def _manifest(tmp_path, payload):
    court_dir = tmp_path / ".court"
    court_dir.mkdir(parents=True, exist_ok=True)
    (court_dir / "config.json").write_text(json.dumps(payload), encoding="utf-8")
    return court_dir


def test_defaults_when_no_manifest_block(tmp_path):
    limits = config.get_context_limits(court_dir=_manifest(tmp_path, {"models": {}}))
    assert limits == {
        "sweet_spot": config.DEFAULT_CONTEXT_SWEET_SPOT,
        "cull_soft": config.DEFAULT_CONTEXT_CULL_SOFT,
        "hard_red_zone": config.DEFAULT_CONTEXT_HARD_RED_ZONE,
    }


def test_global_context_overrides(tmp_path):
    court_dir = _manifest(tmp_path, {"context": {"sweet_spot": 100, "cull_soft": 200, "hard_red_zone": 300}})
    assert config.get_context_limits(court_dir=court_dir) == {
        "sweet_spot": 100, "cull_soft": 200, "hard_red_zone": 300,
    }


def test_per_role_override_only_touches_named_keys(tmp_path):
    court_dir = _manifest(tmp_path, {"context": {
        "cull_soft": 200, "hard_red_zone": 300,
        "per_role": {"gatekeeper": {"hard_red_zone": 900}},
    }})
    assert config.get_context_limits("gatekeeper", court_dir=court_dir) == {
        "sweet_spot": config.DEFAULT_CONTEXT_SWEET_SPOT, "cull_soft": 200, "hard_red_zone": 900,
    }
    # An unnamed role keeps the global values.
    assert config.get_context_limits("serf", court_dir=court_dir)["hard_red_zone"] == 300


def test_invalid_values_fall_back_to_defaults(tmp_path):
    court_dir = _manifest(tmp_path, {"context": {"cull_soft": "lots", "hard_red_zone": -5}})
    limits = config.get_context_limits(court_dir=court_dir)
    assert limits["cull_soft"] == config.DEFAULT_CONTEXT_CULL_SOFT
    assert limits["hard_red_zone"] == config.DEFAULT_CONTEXT_HARD_RED_ZONE
