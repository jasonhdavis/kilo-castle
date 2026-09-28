---
description: Deterministic Multi-Quest Combined Studio — cut an artist-studio worktree from castle tip, merge N quest branches with the established conflict policy, start the freshness-gated runserver, record the studio + session in the ledger
agent: steward
---
Combined Studio Request: $ARGUMENTS

Follow the Combined Studio Protocol (Q-2 — the deterministic formalization of the hand-run recipe exercised on the Q472/Q473/Q412, Q589, and Q617 cohorts):

1. **Determine the Studio Cohort**:
   - From `$ARGUMENTS`, take the explicit comma-separated Quest list (the normal invocation — M'Lord names the quests, e.g. Q617,Q627,Q628), or a `--status/--app/--epic` filter batch.
   - Studio candidates are MoC-audited Quests whose UI Review is PENDING (`TRIBUTE_READY` or `GATE`). Already-approved or headless quests are skipped — they route through `/collect`.

2. **One Composite Command (worktree → merge → env → server → brief → ledger)**:
   ```bash
   python3 -m court.cli studio <QID1>,<QID2>,... [--base castle] [--branch artist/<ids>-ui-studio] [--model "<model>"] [--port <port>] [--standup] [--json] [--no-commit]
   ```
   This deterministically:
   - Gates each candidate (MoC audit present, UI Review PENDING, branch exists, clean worktree, no migration contraband) and refuses unaudited work.
   - Creates the studio worktree `.kilo/worktrees/artist-studio-<ids>` on branch `artist/<ids>-ui-studio`, cut from the castle tip.
   - Merges every candidate branch under the **established conflict policy**:
     - charter paperwork (`.court/quests/**`, task files) → **branch-wins** (the branch holds the full tribute + audit);
     - genuine code overlap → **disclosed union resolution** (both sides' line-level changes via `git merge-file --union`, guarded by conflict-marker + Python-syntax checks) — every union is disclosed to the Artist for live sanity-check;
     - anything unresolvable (add/add, modify/delete) → the branch is **isolated** (merge aborted, tree restored) and stays out of the studio.
   - Copies the gitignored `.env` from the castle root into the fresh worktree (fresh worktrees have no per-checkout env file — the Q617 studio's first boot failed on exactly this).
   - Starts the runserver (`--noreload` semantics via `manage_servers.sh`) behind the **WARN-ONLY freshness gate**: the manifest's `studio.freshness_command` (pb-app: `python3 scripts/db/local_db.py --age`) reports the local production mirror's sync age against the 24h gate — stale is a LOUD WARNING, never an auto-sync.
   - Renders the artist brief to `.kilo/TASK_ARTIST.md` in the studio (easel table with charters + routes, merge disclosures, Volt Pro design-system rules, authority boundaries, per-quest sign-off flow, sync-back steps).
   - Records the studio routing in every merged Quest's Castle Ledger and sets `artist_model` (no Cog Ship stamp, no status change — the studio sits beside the pipeline).

3. **Spawn the Court Artist Session (single unified path — prompting)**:
   - Agent Manager prompting is RETIRED (2026-09-28): it defaults sessions to `steward` mode, lacks agent parameterization, and its launcher times out. Spawn via Kilo CLI instead:
     ```bash
     kilo run --agent artist --model "$(python3 -m court.cli model artist)" --dir .kilo/worktrees/artist-studio-<ids> "$(cat .kilo/worktrees/artist-studio-<ids>/.kilo/TASK_ARTIST.md)"
     ```
     or pass `--standup` to the studio command to spawn + record in one shot, or `cd .kilo/worktrees/artist-studio-<ids> && kilo` (default_agent already `artist`).
   - Record the session on every studio Quest:
     ```bash
     python3 -m court.cli set-field <QID> artist_session_id <session_id>
     ```
   - Present M'Lord the preview URLs and the per-quest route list. **Single-writer**: no Gatekeeper or second session in the studio worktree until royal sign-off.

4. **During the Review (Royal Addendum + merge sanity-check)**:
   - The Artist first live-verifies every union-resolved file in the browser (see the brief's Merge Disclosures), then works the easel with M'Lord.
   - M'Lord may direct additional, unchartered UI changes here; the Artist attributes every polish commit (`Addendum-Quests:` trailer + dated `## Royal Addendum` entry per affected quest charter) so the sync-back carries auditable paperwork into each quest branch.
   - Per-quest approval: scoped tests + Tally update + the quest's `## Master of Coin's Audit` UI Review line flipped to APPROVED.

5. **After Royal Sign-Off — Sync-Back, Then Collection (Steward's)**:
   - Sync the studio branch back into each quest's own worktree branch (the one-convoy pattern from the Q472/Q473/Q412 and Q617 studios):
     ```bash
     python3 -m court.cli studio <QID1>,<QID2>,... --sync-back [--branch artist/<ids>-ui-studio]
     ```
     Sync-back is deliberately non-auto-resolving: a conflict means the quest worktree drifted — it aborts, restores, and reports for a remediation turn.
   - Collection remains the Steward's: levy/collect the signed-off quests into a Cog Ship as usual. The studio branch itself is never promoted directly.

> **Atelier vs Studio**: `/atelier` is the pre-collect **Cog Ship convoy** lane (stamps the convoy, advances to GATE, the Gatekeeper promotes the convoy branch into castle). `/studio` is the **combined artist easel** lane (no stamping, per-quest sync-back, collection via the Steward). Both merge N quest branches into one reviewed state with one artist and one runserver — pick per M'Lord's directive.
