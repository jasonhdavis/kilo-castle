---
description: Studio close-out lifecycle — five guards (sign-off proof, base-drift, convoy-race, labeled cherry-pick extraction, conflict → artist union brief), close-out manifest, gated teardown for a combined studio cohort
agent: steward
---
Studio Close Request: $ARGUMENTS

Run the gated close-out for the named studio cohort (the back half of the combined-studio lifecycle; `/studio --standup` is the front half):

1. **Run the close** (deterministic engine command — guards, extraction, manifest, teardown in one pass):
   ```bash
   python3 -m court.cli studio <QID1>,<QID2>,... --close [--branch artist/<ids>-ui-studio] [--signoff "<note>"] [--drift-threshold N] [--force-union] [--skip-teardown] [--artist-session] [--json]
   ```
   The five guards, in order: dated sign-off proof per quest (a `studio sign-off` ledger stamp, or `--signoff "note"` writes one at invocation); merge-base base-drift ≤ threshold (re-cut recommended beyond it, `--force-union` overrides); convoy-race refusal for quests stamped into a live convoy or merged onto an active `the-gatehouse/*` branch; cherry-pick extraction of the artist's labeled commits (`style(...): <QID> ... royal review ...` / `Addendum-Quests:` trailer) with conflicts aborting clean (never auto-union); conflicts routed to the artist via a generated union brief + `Studio Close: UNION-PENDING` marking.

2. **Read the manifest** (`.court/studio-close/<studio-slug>/manifest.md`, committed with the paperwork): per quest → synced (provenance hashes) / union-pending (brief path) / already-present (verified no-op) / race-blocked, plus the per-quest "approved UI present on branch: YES/NO" check.

3. **Prompt the user ONLY for union-pending rulings** — present each brief (`.court/studio-close/<slug>/<qid>-union-brief.md`) and offer `--artist-session` to stand up a dedicated artist session in the live studio worktree. Never hand-union a conflicted quest.

4. **Teardown** runs inside the command only when the manifest is all-green (or `--override-manifest`): runserver kill → studio worktree moved to the Ashes section while its session still lives → session stop; the studio branch ref is kept. If the command printed `agent_manager move` / `agent_manager stop` commands (sessions it cannot stop from a plain process), execute them verbatim in that order via the agent_manager tool. Physical deletion stays a manual action.
