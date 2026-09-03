---
description: Confirm a Quest for implementation — lock in M'Lord's notes and clear the way for /dispatch without further Audience
agent: steward
---
Quest & Notes: $ARGUMENTS

Follow the Charter Protocol in `.kilo/prompts/steward.md` §"5. Charter":

1. Load the Quest: `court show <id>`.
2. If M'Lord supplied any additional notes or context in `$ARGUMENTS` beyond what's
   already recorded on the Quest, fold them into the Quest record now — append to
   `Goal & Scope` (or a dedicated note) via:
   ```bash
   court set-section <id> "Goal & Scope" --append --content "M'Lord's Charter Notes: <notes>"
   ```
   so the authoritative remit lives on disk, not only in this chat turn.
3. Verify `Goal & Scope` and `Expected Tribute` are both present and concrete given
   those notes. If either is missing or too thin, fill it in now yourself — do not
   charter a Quest with a vague or unusable brief.
4. Advance the Quest:
   ```bash
   court advance <id> PLANNED --note "Chartered: <one-line summary of notes/decision>"
   ```
   (If the Quest already reached `PLANNED` via `/plot`'s own Council confirmation,
   this is a status no-op — just log the additional notes.)
5. **Charter is the green light.** Once chartered, proceed straight to `/dispatch` on
   the Steward's own judgment — no further Audience round is required for this Quest
   unless something genuinely new and material comes up during implementation. Report
   to M'Lord that the Quest is chartered and ready, then either dispatch immediately
   or ask only if M'Lord wants to hold it.

**Charter is the fast lane**: for a well-understood ask, M'Lord can invoke
`/charter <quest id> <notes>` directly on a fresh or lightly-scoped Quest and skip the
full `/plot` Council entirely — the Steward records the notes as authoritative and
moves straight to implementation with the notes in hand.
