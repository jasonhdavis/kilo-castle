---
description: Pack approved Tributes into a Cog Ship bundle (cogship_id), summon Gatekeeper, run integration tests, and promote to castle
agent: steward
---
Arguments: $ARGUMENTS

Follow the Collect Protocol (absorbing gate progression):
> **Steward Principle on Collect Upward**: The Steward does not audit code diffs line-by-line; valuation routing and reading durable completions are paramount. The Steward packs approved Quests into a Cog Ship convoy, summons the Gatekeeper for unified integration testing, and promotes directly to `castle`.

> **Precondition — don't call this on unreviewed `TRIBUTE_READY` Quests**: `/collect` packs Quests Master of
> Coin has already approved, not Quests merely sitting at `TRIBUTE_READY` waiting their turn. In the normal
> flow, `/levy`'s Continuous Summon Chain (Audience checkpoint → Master of Coin → auto-advance to `GATE`)
> invokes this step automatically — you rarely need to run `/collect` by hand from a cold `TRIBUTE_READY`
> queue. If you're triaging `TRIBUTE_READY` Quests directly, dispatch Master of Coin first via `/levy <id>`
> (or the full `/levy` chain); only Quests it has passed (now at `GATE`, or still `TRIBUTE_READY` with a
> `## Master of Coin's Audit` section already on record — Step 1's audit below distinguishes the two) are
> actually ready to be packed here.

1. **Pack the Convoy with One Composite Command** — `court collect` mechanizes the audit,
   Cog Ship stamp, and batch-advance-to-`GATE` in a single call, and REFUSES to pack any
   Quest with no recorded Master of Coin audit (the exact gap that let unaudited Tributes
   slip straight to `GATE` in the past):
   ```bash
   python3 -m court.cli collect [<quest_id_1>,<quest_id_2>...] [--status/--app/--epic] [--cogship <id>]
   ```
   - Omit `quest_ids` to auto-select every Master-of-Coin-approved candidate at
     `TRIBUTE_READY` (or narrow with `--status`/`--app`/`--epic`).
   - Automatically skips (with a stated reason) any Quest that is `PUNISHED`, has a dirty
     working tree, has compliance violations, or has no `## Master of Coin's Audit` content
     on record — route a value-deficient candidate back through `court pillory`, never
     `/reject_tribute`.
   - **Note (Q149, 2026-09-05):** bounded drift at `TRIBUTE_READY` is a warning, not a
     blocking violation — it accumulates naturally while a Quest waits its turn in the
     queue. Only a *dirty* working tree or a genuine merge conflict blocks packing. To
     reconverge a Quest before packing it, run `python3 -m court.cli rebase <id>`
     — mechanical, zero agent turns.
   - This one command replaces the old manual `audit` → `stamp` → per-Quest `advance ...
     GATE` sequence and prints the exact Gatekeeper dispatch NEXT STEPS for step 3 below.

3. **Summon the Gatekeeper & Execute Unified Integration**:
   - **Gatehouse routing is by convoy size (2026-09-05 re-architecture — the persistent
     `the-gatehouse/north|south|east|west` named stations are retired):** `agent_manager` has
     no way to attach a fresh session to an already-existing, sessionless worktree — only
     create new — which is exactly why those 4 stations kept going stale (seen up to 751
     commits behind castle) between convoys: nothing fast-forwards a dormant worktree with
     no session running in it.
     - **Convoy of 1 Quest**: skip a separate Gatehouse worktree entirely. Prompt that
       Quest's own EXISTING worktree session via `agent_manager` `action: "prompt"` to act
       as Gatekeeper directly there — merge castle in, run the full suite, promote on a
       clean pass. Nothing to batch, so no reason for a separate integration point.
     - **Convoy of >1 Quest** (the common case): spawn ONE brand-new ephemeral worktree via
       `agent_manager` `mode: "worktree"`, `branchName: "the-gatehouse/<cogship_id>"`, off
       castle's current tip — never an old/idle named station. Prompt that new session to
       act as Gatekeeper. Tear the worktree down (Ashes/stop) once promoted; it is scoped to
       this one convoy only, not meant to persist for reuse.
     - **NEVER** run Gatekeeper as a background task, background process, or on castle.
     - Gatekeeper is a **Gemini 3.7 Flash** class agent (`openrouter/google/gemini-3.7-flash`).
     - Allowed to spawn sequential non-background tasks (`background: false`) for integration verification.
   - Record metadata:
     ```bash
     python3 -m court.cli set-field <id> gatekeeper_session_id <session_id>
     python3 -m court.cli set-field <id> gatekeeper_model "openrouter/google/gemini-3.7-flash"
     ```
   - **Unified Test Run & Fault Isolation**: Gatekeeper merges the Cog Ship pack into the target
     worktree (the solo Quest's own branch for a size-1 convoy, or the fresh ephemeral
     `the-gatehouse/<cogship_id>` branch for a size->1 convoy) and runs the unified test suite. If
     tests fail, Gatekeeper isolates the offending commit/Quest, rejects it back to `WORKING`, and
     prompts its Serf with failure traceback and remediation notes.

4. **Promote Passing Quests & Compile Cog Ship Manifest**:
   - For passing Quests, Gatekeeper promotes the verified branch directly into `castle`, advances
     status to `READY_TO_RAZE` ("Ready to Raze"), and queues worktrees for **Ashes** (including the
     ephemeral Gatehouse worktree itself, for a size->1 convoy — it does not persist between convoys).
   - Generate the full deployment manifest via `python3 -m court.cli ship --cogship <id>` (or default rollup pillars: Bard, Coffers, Tally, Atone, Murmur).
   - **Teardown is the Steward's job, not the Gatekeeper's own.** The Gatekeeper never calls
     `agent_manager stop` on its own session (same self-stop timing risk as Master of Coin —
     see `AGENTS.md`). It reports the promoted commit hash(es) back; the Steward runs
     `python3 -m court.cli fork-teardown-list` to confirm the promotion actually landed
     on `castle` and get the exact next commands. For an ELIGIBLE convoy: `agent_manager` `move`
     the session into **Ashes** first (while it's still alive), then `agent_manager` `stop` it —
     never the reverse, and never `git worktree remove` (M'Lord prunes the directory by hand).

5. **Report to M'Lord (Cog Ship Voyage Report)**:
   - Present the synthesized deployment summary using `status_label()` names:
     - 🏰 **Collected / Merged into Castle** (`READY_TO_RAZE` / Ready to Raze) + Cog Ship convoy manifest summary.
     - 🛡️ **Under Gatekeeper Testing** (`GATE` / Collecting Tribute).
     - ↩️ **Returned to Working** (`WORKING` / Working - rejected Quests).
   - **Pipeline Look-Ahead**: List what is sitting at Working with complete tribute ready for `/levy` next.
