---
description: Send a Quest to the pillory, freezing it as PUNISHED with Decrees for a chartered successor (Master of Coin rejection path)
agent: steward
---
Quest: $ARGUMENTS

Send a Quest to the pillory. This is the Court's **only** failure path: there is no
"return to WORKING" from a rejected audit, and a punished Quest's worktree is never
re-opened — a brand-new Quest + worktree is always chartered from the Decrees
(the one exception being the Gatekeeper's own `/reject_tribute` for mechanical
integration fixes, which does NOT go through the pillory).

1. **Courtesy Proof-of-Landing Check (automatic)**:
   `python3 -m court.cli pillory <id> --reason "<one-line reason>" --decrees "<numbered decrees, one line each, semicolon-separated>" [--successor <new_id>]`
   - The CLI itself first scours `castle`, `main`, and any live `the-gatehouse/*` convoy
     branches for proof the work actually landed (`git_ops.check_proof_of_landing`).
   - If proof is found, it skips the ceremony: status stays as-is, a Castle Ledger note
     is written, done. No new Quest needed.

2. **If Not Landed — the Freeze (automatic)**:
   The Quest is set to `PUNISHED` (side-state; label "Punished") and a
   `## Judgement of the Condemned` section is written into its Charter with:
   `- **Reason:**`, `- **Decrees Issued:**`, `- **Successor Quest:**`,
   `- **Frozen Worktree:**` (read-only; no Serf ever re-enters it), `- **Raze Together With:**`.
   Linkage fields: `pilloried_by` on the punished Quest, `pillory_of` on its successor.

3. **Charter the Successor (Steward's follow-through)**:
   - Create the successor Quest (`/quest` or `/plot`), seeding its **The Kingdom Requires**
     directly from the Decrees (keep what passed; discard what failed; if the audit found
     duplication, the Decrees name the exact existing code path the successor must reuse).
   - Wire the pair (if `--successor` was not passed at pillory time) via the dedicated
     composite, never raw `set-field` (`pilloried_by`/`pillory_of` are blocklisted fields
     that will error on `set-field`):
     `python3 -m court.cli charter <successor_id> --pillory-of <punished_id>`
   - Dispatch the successor Serf into a **fresh worktree**; the punished worktree stays
     frozen and visible ("on public display"), never silently deleted.

4. **Joint Teardown (automatic later)**:
   `court raze` refuses to raze a `PUNISHED` Quest alone; once its successor reaches
   `READY_TO_RAZE`/`DONE`, both are razed together in the same pass (recursing via
   `pillory_of`). No merge checks run for the punished Quest — its code was rejected,
   never promoted.

5. **Report to M'Lord**:
   State the Reason, the Decrees, and the successor Quest ID. The Judgement of the
   Condemned section is the durable record — the chat summary is only the herald's cry.
