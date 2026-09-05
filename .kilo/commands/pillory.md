---
description: Freeze a Quest as PUNISHED with Decrees for a chartered successor — the full judgment-call rejection path
agent: steward
---
Quest: $ARGUMENTS

Send a Quest to the pillory. This is reserved for a genuine judgment call — the
work is the wrong value, the wrong scope, or a duplicate of something that already
exists — not for a routine, fixable mistake. It is a distinct path from a mechanical
rejection: there is no "return to WORKING and let the same Serf patch it" outcome
here, and a punished Quest's worktree is never re-opened. A brand-new Quest +
worktree is always chartered from the Decrees instead. (The Gatekeeper's own
same-worktree `/reject_tribute` path for mechanical integration failures does NOT
go through the pillory — see `.kilo/commands/reject_tribute.md`.)

1. **Freeze the Quest:**
   ```bash
   court pillory <id> --reason "<one-line reason>" --decrees "<numbered decrees, one line each, semicolon-separated>" [--successor <new_id>]
   ```
   - This sets the Quest's status to `PUNISHED` (a side-state; label "Punished") and
     writes a `## Judgement of the Condemned` section into its Charter with the Reason,
     the Decrees Issued, the Successor Quest (once known), and the Frozen Worktree path
     (read-only; no Serf ever re-enters it).
   - Linkage fields: `pilloried_by` is set on the punished Quest once a successor
     exists; `pillory_of` is set on the successor pointing back at the punished Quest.

2. **Charter the Successor (Steward's follow-through):**
   - Create the successor Quest (`/quest` or `/plot`), seeding its Goal & Scope
     directly from the Decrees: keep what passed, discard what failed, and if the
     rejection was a duplication finding, name the exact existing code path the
     successor must reuse instead of rebuilding it a third time.
   - Wire the pair if `--successor` was not passed at pillory time:
     ```bash
     court set-field <punished_id> pilloried_by <successor_id>
     court set-field <successor_id> pillory_of <punished_id>
     ```
   - Dispatch the successor's Serf into a **fresh worktree**; the punished worktree
     stays frozen and visible, never silently deleted.

3. **Joint Teardown (later, at raze time):**
   A `PUNISHED` Quest is never razed alone — once its successor reaches
   `READY_FOR_TEARDOWN`/`DONE`, both are razed together in the same pass (following the
   `pillory_of` linkage). No merge checks run for the punished Quest; its code was
   rejected, never promoted.

4. **Report to M'Lord:**
   State the Reason, the Decrees, and the successor Quest ID once chartered. The
   Judgement of the Condemned section is the durable record — the chat summary is only
   a courtesy notice.
