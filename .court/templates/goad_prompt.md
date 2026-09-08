## Goad Prompt

You are being goaded because Quest `<QUEST_ID>` is stationed in `WORKING` and either
(a) your prior session went idle without finishing the job, or (b) your prior session has
already exited/closed and this is a **brand-new session** picking the worktree back up cold.
Treat this as a cold start: do not assume any prior in-context state survived.

**Step 0 — Re-orient (mandatory, do this first, every time):**
1. `git branch -m <canonical_branch>` if the branch name is flat-hyphenated instead of the
   canonical slash-folder form (see `AGENTS.md` Branch topology).
2. `git status --porcelain` — confirm the working tree state you're actually starting from.
3. `python3 -m court.cli show <QUEST_ID>` — read `# Expected Tribute` (checklist),
   `# Tribute Rendered` (narrative writeup), and current `status:`.

**Step 1 — Diagnose which of these two states you're actually in:**

- **Case A — Tasks checklist is 100% checked off, working tree is clean, but `# Tribute
  Rendered` is empty (or missing sections) and `status:` is still `WORKING`.**
  1. Re-verify the work: re-run whatever test command the charter specifies.
  2. Write the full `# Tribute Rendered` section (Ballad, Tribute, Tally, Penance, Audience,
     Humble Opinion) via:
     `python3 -m court.cli set-section <QUEST_ID> "Tribute Rendered" --content "<full markdown>"`
  3. Run `git merge castle` once and confirm `↓0` before advancing.
  4. Advance the ledger yourself:
     `python3 -m court.cli advance <QUEST_ID> TRIBUTE_READY --note "Tribute rendered, deferred rebase complete."`

- **Case B — Tasks checklist is not 100% and/or the working tree has outstanding scope left.**
  Pick up the charter and continue implementing the remaining unchecked items to completion.
  Once complete, follow Case A steps 2-4 above.

**Step 2 — Never leave silently.** Before your session goes idle again, you must have advanced the Quest to `TRIBUTE_READY` or left concrete checked-off progress with an explanation of what remains.

**Step 3 — Confirm you were heard.** Reply in this session with which Case you diagnosed and what you did about it.
