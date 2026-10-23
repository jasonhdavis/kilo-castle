"""Worker-prompt contract guards (2026-10-23 pipeline defect report).

Three reusable defects surfaced in pb-app:
1. `court dispatch` silently stripped charter bodies (parser/eventlog — covered
   by test_eventlog_tribute_integrity + test_section_roundtrip).
2. GLM-5.3-Flash workers died on single very large file writes — the worker
   system prompts must mandate chunked writes up front.
3. A promoted convoy could not be handed back to a Gatekeeper — collect cannot
   re-mint, and a bare `kilo run --agent gatekeeper` dies (missing `--auto`).
   Covered by test_collect_regate; this file pins the operator-facing text.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_serf_and_scout_prompts_mandate_chunked_writes():
    for rel in (
        "court/agents/serf.md",
        "court/agents/scout.md",
        ".kilo/agent/serf.md",
        ".kilo/agent/scout.md",
    ):
        text = _read(rel)
        assert "CHUNKED LARGE-FILE WRITES" in text, rel


def test_gatekeeper_prompt_documents_hand_back():
    for rel in ("court/agents/gatekeeper.md", ".kilo/agent/gatekeeper.md"):
        text = _read(rel)
        assert "Hand-Back / Re-gate" in text, rel
        assert "--regate" in text, rel


def test_printable_kilo_run_instructions_are_headless_safe():
    """No operator-facing instruction may print a bare `kilo run --agent ...`."""
    offenders = []
    for pattern in ("court/**/*.py", "court/**/*.md", ".kilo/commands/*.md", "AGENTS.md"):
        for p in REPO.glob(pattern):
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                if "kilo run --agent" in line and "--auto" not in line:
                    offenders.append(f"{p.relative_to(REPO)}:{i}: {line.strip()}")
    assert not offenders, "bare kilo run invocations:\n" + "\n".join(offenders)
