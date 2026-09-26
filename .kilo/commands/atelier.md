---
description: Roll up multiple UI reviews into one Cog Ship — merge branches pre-integration-test and spawn the Court Artist on the merged convoy branch
agent: steward
---
UI Convoy Request: $ARGUMENTS

Follow the Royal UI Atelier Protocol:

1. **Determine the Convoy Batch**:
   - From `$ARGUMENTS`, take an explicit comma-separated Quest list, or determine the batch of UI-review-pending Quests (`python3 -m court.cli list --status TRIBUTE_READY,GATE` filtered to quests whose Master of Coin audit records `UI Review: PENDING`).
   - Atelier candidates are quests the Master of Coin already passed but whose UI review is still PENDING. Already-approved or headless quests belong to `/collect`, never here.

2. **One Composite Command (merge → stamp → server → prompt)**:
   ```bash
   python3 -m court.cli atelier <QID1>,<QID2>,... [--cogship <id>] [--model "<model>"] [--json]
   ```
   This deterministically:
   - Gates each candidate (audit present, UI Review PENDING, clean tree, no migration contraband) and refuses unaudited tribute.
   - Stamps the quests onto a Cog Ship convoy and advances them to `GATE`.
   - Creates the ephemeral convoy worktree `.kilo/worktrees/the-gatehouse-<cogship_id>` on branch `the-gatehouse/<cogship_id>`, fast-forwards to `castle`, and merges each candidate branch in (isolating any branch that conflicts).
   - Configures the worktree for the `artist` agent (single-writer) and starts its runserver.
   - Renders `.court/templates/court_artist_convoy_prompt.md` (Royal Addendum protocol, single-writer rule, per-quest routes) and records `artist_model` on every packed quest.

3. **Spawn the Court Artist Session in the Convoy Worktree**:
   - Spawn a dedicated session bound to the convoy branch `the-gatehouse/<cogship_id>` (Agent Manager `mode: "worktree"`, `branchName: "the-gatehouse/<cogship_id>"`, using the `--json` task payload — or `cd .kilo/worktrees/the-gatehouse-<cogship_id> && kilo`, where `default_agent` is already `artist`).
   - Record the session on each packed quest:
     ```bash
     python3 -m court.cli set-field <QID> artist_session_id <session_id>
     ```
   - Present M'Lord the preview URL and the per-quest route list.
   - **Single-writer**: do NOT stand up the Gatekeeper yet — the artist owns the worktree until royal sign-off.

4. **During the Review (Royal Addendum)**:
   - M'Lord may direct additional, unchartered UI changes here; this is the only sanctioned venue. The artist must attribute every polish commit (`Addendum-Quests:` trailer + dated `## Royal Addendum` entry per affected quest charter).
   - On per-quest approval the artist updates each quest's `## Master of Coin's Audit` UI Review line to APPROVED.

5. **After Royal Sign-Off — Handover to the Gatekeeper (sequenced, same worktree)**:
   - Verify the artist committed everything and stopped (clean tree, no live artist session).
   - Run the unified suite via the ENGINE: `python3 -m court.cli runsuite --cogship <cogship_id> --dir .kilo/worktrees/the-gatehouse-<cogship_id>`.
   - Stand up the Gatekeeper in the same worktree (candidate branches are already merged; it runs the suite, promotes the clean convoy into `castle`, advances passing quests to `READY_TO_RAZE`, and packs the manifest via `court ship`).
   - If the Gatekeeper isolates/rejects a quest, addendum polish entangled with that quest's files is reverted with it (`gatekeeper_review_prompt.md` → Step 2c, rule 3).

> **Atelier vs Studio**: this command is the pre-collect **Cog Ship convoy** lane (stamp + GATE + Gatekeeper promotion of the convoy branch). For the **combined artist easel** lane — no stamping, per-quest sync-back after sign-off, collection via the Steward — use `/studio <QID1>,<QID2>,...` (`court studio`, the Q-2 deterministic Combined Studio with the charter-paperwork → branch-wins / disclosed-union conflict policy and the WARN-ONLY freshness gate).
