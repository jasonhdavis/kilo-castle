"""Unit tests for the cheap cold-start handoff helpers (court.handoff)."""
import subprocess

from court import handoff


class _Quest:
    def __init__(self, qid, tribute):
        self.id = qid
        self.body_sections = {"Expected Tribute": tribute}


TRIBUTE = """- [x] Already done thing
- [ ] Install the widget
- [ ] Write the report and paste the exact command output
- [~] Cancelled edge case
- [ ] nested
  continuation line
"""


def test_unchecked_items_extracts_and_clips():
    q = _Quest("Q1", TRIBUTE)
    items = handoff.unchecked_items(q, limit=10, width=20)
    assert items == [
        "Install the widget",
        "Write the report and" + "\u2026",  # clipped to 20 chars + ellipsis
        "nested",
    ]
    assert all(not it.startswith("- [") for it in items)


def test_unchecked_items_respects_the_limit():
    body = "\n".join(f"- [ ] item {i}" for i in range(50))
    items = handoff.unchecked_items(_Quest("Q1", body), limit=3)
    assert len(items) == 3


def test_unchecked_items_survives_a_missing_section():
    class Bare:
        id = "Q9"
        body_sections = {}

    assert handoff.unchecked_items(Bare()) == []


def test_is_bloated_disabled_by_zero_limit():
    assert handoff.is_bloated("ses_anything", 0) == (False, None)


def test_is_bloated_unknown_session_is_not_bloated():
    assert handoff.is_bloated("ses_definitely-not-a-real-session-id", 1) == (False, None)


def test_session_context_tokens_rejects_non_session_ids():
    assert handoff.session_context_tokens("") is None
    assert handoff.session_context_tokens("../../etc/passwd") is None
    assert handoff.session_context_tokens(None) is None


def test_cold_start_prompt_is_short_directive_and_bounded():
    quests = [_Quest("Q1", TRIBUTE)]
    prompt = handoff.cold_start_prompt(quests, prior_tokens=250_000)
    assert "Q1" in prompt
    assert "250,000 tokens" in prompt
    assert "Install the widget" in prompt
    assert "Already done thing" not in prompt          # finished work not restated
    assert "Context discipline" in prompt
    assert "Never dump a whole file" in prompt
    # Bounded: the prompt must not scale with a huge checklist.
    big = _Quest("Q2", "\n".join(f"- [ ] task {i} " + "x" * 400 for i in range(200)))
    assert len(handoff.cold_start_prompt([big])) < 4000


SOFT, HARD = 350_000, 750_000


def test_continuation_decision_unknown_context_never_cold_starts():
    cold, why = handoff.continuation_decision(None, SOFT, HARD, True)
    assert cold is False and "unknown" in why


def test_continuation_decision_under_soft_continues():
    cold, why = handoff.continuation_decision(100_000, SOFT, HARD, True)
    assert cold is False and "under the soft cap" in why


def test_continuation_decision_soft_is_gated_on_a_committed_tree():
    # Past soft but uncommitted: keep the session so a fresh start cannot lose work.
    cold, why = handoff.continuation_decision(400_000, SOFT, HARD, checkpointed=False)
    assert cold is False and "uncommitted work" in why
    # Past soft and committed: cull.
    cold, why = handoff.continuation_decision(400_000, SOFT, HARD, checkpointed=True)
    assert cold is True and "cull opportunity" in why


def test_continuation_decision_hard_red_zone_ignores_checkpoint():
    cold, why = handoff.continuation_decision(800_000, SOFT, HARD, checkpointed=False)
    assert cold is True and "red zone" in why


def test_continuation_decision_zero_thresholds_disable_rules():
    assert handoff.continuation_decision(9_000_000, 0, 0, True)[0] is False
    assert handoff.continuation_decision(400_000, 0, HARD, True)[0] is False   # soft off
    assert handoff.continuation_decision(800_000, 0, HARD, True)[0] is True    # hard on


def test_worktree_is_clean_reports_false_for_non_repo(tmp_path):
    assert handoff.worktree_is_clean(tmp_path) is False
    assert handoff.worktree_is_clean(None) is False


def test_worktree_is_clean_tracks_committed_vs_dirty(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True,
                       capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    git("add", "f.txt")
    git("commit", "-qm", "init")
    assert handoff.worktree_is_clean(tmp_path) is True
    # Engine-managed untracked scaffolding must not count as dirty.
    (tmp_path / "untracked-scaffold.py").write_text("x", encoding="utf-8")
    assert handoff.worktree_is_clean(tmp_path) is True
    # A modified tracked file does.
    (tmp_path / "f.txt").write_text("y", encoding="utf-8")
    assert handoff.worktree_is_clean(tmp_path) is False
