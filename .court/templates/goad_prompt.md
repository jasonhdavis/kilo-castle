# Goad Prompt Template — Idle/Exited Serf Recovery Diagnostic

Fill in `<QUEST_ID>` and `<canonical_branch>` before sending this to an existing
worktree session via `agent_manager` (`action: "prompt"`), or as the initial
prompt when starting a brand-new session bound to that same worktree/branch if
no live session remains (a Serf's terminal exiting after it finishes its
checklist is routine and does not by itself mean the Quest is done — see Case
A below).

---

You are being goaded because Quest `<QUEST_ID>` is sitting in `WORKING` and either
(a) your prior session went idle without finishing the job, or (b) your prior session has
already exited/closed and this is a **brand-new session** picking the worktree back up cold.
Either way, treat this as a cold start: do not assume any prior in-context state survived.

**Step 0 — Re-orient (mandatory, do this first, every time):**
1. `git branch -m <canonical_branch>` if the branch name is flat-hyphenated instead of the
   canonical slash-folder form (see `AGENTS.md` / this repo's branch topology rules).
2. `git status --porcelain` — confirm the working tree state you're actually starting from.
3. `court show <QUEST_ID>` — read `# Expected Tribute` (checklist), `# Tribute Rendered`
   (narrative writeup), and the current `status:` frontmatter field. This is the ground
   truth, not chat memory.

**Step 1 — Diagnose which of these two states you're actually in:**

- **Case A — Tasks checklist is 100% checked off, working tree is clean, but `# Tribute
  Rendered` is empty (or missing sections) and `status:` is still `WORKING`.** This means a
  prior session did the code work and checked off the boxes but never wrote up the tribute
  narrative and never advanced the ledger. **The code is not the missing piece — the
  paperwork is.** Do this:
  1. Re-verify the work is actually real: re-run whatever test command the Quest's Expected
     Tribute specifies, and skim the actual diff (`git log --oneline -10`,
     `git diff castle...HEAD --stat`) to confirm it matches the checklist before you vouch
     for it in writing.
  2. Write the full `# Tribute Rendered` section (Ballad, Tribute, Penance, Audience,
     Humble Opinion — same structure as every other rendered Quest) via:
     `court set-section <QUEST_ID> "Tribute Rendered" --content "<full markdown>"`
  3. Run `git merge castle` once (the deferred-rebase step) and confirm zero commits behind
     `castle` before advancing.
  4. Advance the ledger yourself — nothing else does this for you:
     `court advance <QUEST_ID> REVIEW --note "Tribute rendered, deferred rebase complete."`

- **Case B — Tasks checklist is not 100% (e.g. `[Tasks: 0/5]`) and/or the working tree has
  outstanding scope left.** The prior session genuinely did not finish. Pick up the Goal &
  Scope / Expected Tribute checklist and continue implementing the remaining unchecked items
  to completion — do not just re-render tribute for work that was never done. Once genuinely
  complete, follow Case A steps 2–4 above.

**Step 2 — Never leave silently.** Before your session goes idle again, you must have done
one of:
- Advanced the Quest to `REVIEW` (Case A complete), or
- Left concrete, checked-off progress against the Expected Tribute checklist with the scope
  still genuinely incomplete (Case B, in progress) — in which case say explicitly what
  remains, so the next goad (or `/levy`) doesn't have to re-diagnose from scratch.

**Step 3 — Confirm you were heard.** Reply in this session with which Case you diagnosed and
what you did about it. This session's response is read by the calling `/goad` or `/levy` run
to verify the goad actually landed — a rebase-only "already up to date, no drift" reply does
**not** count as having goaded this quest.
