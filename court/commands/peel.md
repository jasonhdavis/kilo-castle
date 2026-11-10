---
description: Withdraw a promoted Quest from the trunk — revert its promotion commit, record the peel, and re-pack the surviving convoy
agent: steward
---
Arguments: $ARGUMENTS

Withdraw a Quest whose code was **already promoted into `castle`** — a pillory
after promotion, a royal ruling, or a discovery that the merged code is wrong.
This is the gatehouse's reversibility verb: promotion is executed per branch, so
a Quest's commits travel into the trunk as part of a convoy; without a peel the
only ways out are to lie about the ruling (un-pillory) or hide the branch from
the guard, and neither removes the rejected code from what ships.

**Peel off, then re-pack.** The peel reverts the Quest's promotion commit on the
trunk (content-level, so the rejected code cannot ship), records the withdrawal
in `.court/peels/<QUEST>.json`, and supersedes the convoy's green suite proof.
`court collect` clears the peel record when the Quest is packed again, so the
Quest is only ever reinstated through a real re-pack.

1. **Identify the Quest and why it is being withdrawn**:
   ```bash
   python3 -m court.cli peel <quest_id> --reason "royal ruling: <one line>"
   ```
   Resolution order for the commit to revert:
   - `--commit <sha>` (explicit; use this when the Quest's convoy was entangled
     with another Quest's promotion, so no single merge is its own);
   - the Quest's recorded `cogship_promoted_commit`, when that commit is the
     Quest's own merge;
   - the newest trunk merge whose subject names the Quest's branch.

   When no commit can be attributed without guessing, the command **fails
   closed** instead of reverting a sibling Quest's code. Find the Quest's own
   code commit with:
   ```bash
   git log --oneline castle --grep <quest_number>
   ```

2. **Preview the plan** (no git mutation):
   ```bash
   python3 -m court.cli peel <quest_id> --dry-run
   ```

3. **Peel**:
   ```bash
   python3 -m court.cli peel <quest_id> --reason "<why>"
   ```
   This creates two commits in the trunk worktree: the revert and its peel
   record. The convoy's suite proof is moved aside as
   `.court/suites/<cogship>.json.superseded-<stamp>` — the green proof was
   stamped against a tree that still carried the withdrawn Quest.

4. **Re-pack and re-verify the survivors**:
   ```bash
   python3 -m court.cli collect <remaining_quest_ids> --cogship <new_or_same_cogship>
   ```
   `collect` records the new pack allow-list and clears any stale peel record.
   The surviving pack must then pass the unified suite (`court runsuite`) and
   the promotion guard (`court verify-convoy`) again before it promotes.

5. **Ship without the withdrawn Quest**:
   ```bash
   python3 -m court.cli ship --confirm
   ```
   The trunk invariant now treats the peeled Quest as withdrawn (its revert is
   an ancestor of the trunk), so a pilloried Quest no longer blocks the rest of
   the convoy from shipping.

Inspect records and their validity with:
```bash
python3 -m court.cli peel --list
```
A record whose revert commit is no longer an ancestor of the trunk is reported
as **stale** and does not excuse the Quest from the trunk invariant.
