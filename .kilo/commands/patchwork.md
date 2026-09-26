---
description: Fast-track hotfix pipeline - one-shot intake, serf dispatch, inline test verification, and direct promotion
agent: steward
---
Hotfix Request: $ARGUMENTS

Follow the Patchwork Hotfix Protocol:

1. **Intake & Standup (One Composite Command)**:
   - For a new hotfix, determine `app`, `concern`, and technical fix description from `$ARGUMENTS`.
   - Execute:
     ```bash
     python3 -m court.cli patchwork --app <app> --concern <concern> \
       --title "<title>" --goal "<technical specification>" [--test-cmd "<test command>"]
     ```
   - This executes one-shot creation, auto-charters the Quest under section `Bug fix` with tag `hotfix`, commits the charter to `castle`, cuts the canonical `quest/<id>-<concern>` branch, provisions the worktree under `.kilo/worktrees/`, sets `.kilo/kilo.json` (`default_agent: "serf"`), and spawns the focused Serf worker session via Kilo CLI.

2. **Serf Toil & Handoff in Worktree**:
   - The Serf worker implements the minimal fix (<= 100 lines diff, <= 3 files).
   - Runs in-tree test verification.
   - Syncs with castle (`git merge castle --ff-only` / zero drift).
   - Renders 5-part Tribute under `## Tribute Rendered` and advances to `TRIBUTE_READY`.

3. **Inline Gatehouse Verification & Promotion**:
   - Run:
     ```bash
     python3 -m court.cli patchwork <id> --collect
     ```
     (or `python3 -m court.cli collect <id> --hotfix`).
   - Automatically populates inline Master of Coin audit, checks diff boundaries, and packs into Cog Ship at `GATE`.
   - To promote directly into `castle`:
     ```bash
     python3 -m court.cli patchwork <id> --promote
     ```
     This executes the verification suite in-tree, fast-forwards/merges into `castle`, stamps the Cog Ship manifest, and advances to `READY_TO_RAZE`.

4. **Teardown & Deployment Summary**:
   - View Cog Ship deployment vector: `python3 -m court.cli ship`
   - Move worktree to Ashes and archive record: `python3 -m court.cli raze <id>`
