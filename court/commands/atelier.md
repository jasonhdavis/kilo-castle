---
description: Royal UI Atelier — merge UI-review-pending Quests into the SINGLE standing studio (one merged worktree + dev server) for review and per-Quest patronage
agent: steward
---

UI Atelier Request: $ARGUMENTS

Follow the Standing Atelier Protocol — the single UI-review path:

1. **Determine the Batch**:
   - From `$ARGUMENTS`, take an explicit comma-separated Quest list, or the batch of UI-review-pending Quests (`python3 -m court.cli list --status TRIBUTE_READY,GATE` filtered to Quests whose Master of Coin audit records `UI Review: PENDING`).
   - Atelier candidates are Quests the Master of Coin already passed but whose UI review is still PENDING. Already-approved or headless Quests belong to `/collect`, never here; a single Quest can use the lighter `/artist <id>`.

2. **Merge Into the Standing Studio (one composite command)**:
   ```bash
   python3 -m court.cli atelier <QID1>,<QID2>,... [--standup] [--model "<model>"] [--json]
   ```
   Bare `court atelier` (the default; `--standing` is an accepted alias) deterministically:
   - Ensures the ONE long-lived worktree `.kilo/worktrees/artist-atelier` on the stable branch `artist/atelier`, cut from `castle`, with a single merged dev server.
   - Merges each candidate branch into the merged view (charter paperwork branch-wins; a genuine code overlap is disclosed-union or isolated per-Quest and reported).
   - Records each Quest in the atelier manifest (`.court/atelier/manifest.json`) and logs the merge to its Castle Ledger.
   - Starts/keeps the single merged runserver and writes the artist brief to `<worktree>/.kilo/TASK_ARTIST.md`.
   - **Single-writer sequencing**: a live artist turn in the worktree refuses a merge-in (queue it for when the artist is idle, or pass `--force`). The merge is an engine/Steward action, never the artist's own.

3. **Summon the Court Artist in the Merged View** (`--standup`, or the printed Kilo CLI line):
   ```bash
   kilo run --auto --agent artist --model "$(python3 -m court.cli model artist)" \
     --dir .kilo/worktrees/artist-atelier "$(cat .kilo/worktrees/artist-atelier/.kilo/TASK_ARTIST.md)"
   ```
   - The artist confirms each Quest's code is actually merged and its view renders, inspects the tribute against its charter, and summarizes the pages to review. It may write view code AND the service functions behind it.
   - Record the session on each merged Quest:
     ```bash
     python3 -m court.cli set-field <QID> artist_session_id <session_id>
     ```
   - Present M'Lord the single preview URL and the per-Quest route list.

4. **During the Review (Royal Addendum)**:
   - M'Lord may direct additional, unchartered UI changes here; this is the only sanctioned venue. The artist MUST attribute every polish commit (`style(...): <QID> royal review` subject and/or `Addendum-Quests:` trailer) so each Quest's approved revision is pinnable.
   - On per-Quest approval the artist sets that Quest's `## Master of Coin's Audit` UI Review line to APPROVED.

5. **Patronize Each Quest Independently (deferred, mechanical extraction)**:
   ```bash
   python3 -m court.cli patronize <QID> --by "M'Lord" --note "<where said>"
   ```
   - Pins the Quest's approved revision as the ordered set of its OWN artist-review commit SHAs — never the atelier tip, so another Quest's in-progress work can never ride along.
   - Writes the written UI sign-off the gates read and queues the Night's Watch to collect it. Collection replays only the pinned commits onto the Quest's own branch, then the Gatekeeper integrates that Quest through its own branch.

6. **Manage the Studio**:
   - `python3 -m court.cli atelier --list` — membership and per-Quest patronage state.
   - `python3 -m court.cli atelier --reap` — tear the worktree down (branch ref kept).

> **Standing Atelier vs Combined Studio vs Convoy**: `/atelier` is the standing studio (above) — the single, preferred UI-review path. `/studio <QID1>,<QID2>,...` (`court studio`) is the per-cohort **combined easel** lane (no stamping, per-quest sync-back, collection via the Steward). `/convoy <QID1>,<QID2>,...` (`court atelier --convoy`) is the legacy pre-collect **Cog Ship convoy** lane (stamps the convoy and advances the Quests to `GATE`; the Gatekeeper promotes the convoy branch after the unified suite). Prefer the standing atelier; the studio and convoy remain for compatibility.
