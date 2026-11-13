---
id: Q002-Castle-Nightwatch-Convoy-Backpressure
title: Night's Watch: bound convoy spend across ticks and terminate the zero-delta regate livelock
kind: quest
app: castle
concern: nightwatch-convoy-backpressure
parent_epic: 
section: Bug fix
tags: Bug fix
status: PLANNED
cogship_id: 
cogship_station: 
cogship_promoted_commit: 
task_file: 
pillory_of: 
pilloried_by: 
scout_of: 
branch: quest/q002-castle-nightwatch-convoy-backpressure
worktree: 
serf_session_id: 
serf_model: 
master_of_coin_session_id: 
master_of_coin_model: 
gatekeeper_session_id: 
gatekeeper_model: 
artist_session_id: 
artist_model: 
vassal_session_id: 
created_at: 2026-11-13T12:50:38Z
updated_at: 2026-11-13T13:24:30Z
razed_at: 
---

# Q002-Castle-Nightwatch-Convoy-Backpressure — Night's Watch: bound convoy spend across ticks and terminate the zero-delta regate livelock

## Castle Ledger

- **2026-11-13T12:50:38Z** — - → OPEN: Quest created
- **2026-11-13T12:50:41Z** — OPEN → PLANNED: Chartered via composite `court charter`

## The Kingdom Requires

## Context (verified 2026-11-13)

`court/nightwatch.py` — the deterministic fleet supervisor — can drive the host into memory thrash by running more gatehouse integration convoys at once than the machine can afford, and by re-gating the same already-promoted Quests forever.

Observed 2026-11-13T12:42Z on the pb-app board: four gatehouse convoys live simultaneously (`cogship-588`, `-591`, `-592`, `-593`) plus a Master-of-Coin audit — 4 `kilo run` sessions and 7 `pytest` processes — with `load averages 23.58 12.93 11.23` on 10 cores and `vm.swapusage` at 5060 MB / 6144 MB (82 %). That is worse than the 2026-11-12 load-19 incident the load-shed was meant to prevent.

### Defect A — convoy admission is per-tick, so convoys accumulate across ticks

`plan()` derives `capacity = policy.max_workers - busy_workers` once per tick from `live_kilo_workers()` — a live `ps` census of `kilo run` processes — and gates multi-Quest convoys on a per-tick local `gatehouse_slot = 1`. Nothing durable records "a convoy is already integrating", so:

- `court collect --standup` / `court collect --regate --standup` spawn asynchronously and return before the new Gatekeeper session appears in the `ps` census, so a later tick's census can still report free capacity;
- the real RAM consumer is the ~18,000-test `pytest` run, which is not a `kilo run` session and is invisible to `live_kilo_workers()`;
- a suite runs for tens of minutes while the watch ticks roughly every six minutes.

Net effect: `--max-workers 3` did **not** bound the convoy count; the only convoy-specific governor was the per-tick `gatehouse_slot`, which resets to 1 every tick.

### Defect B — the raze/regate loop never terminates

`_bucket_raze()` routes any `READY_TO_RAZE` Quest whose branch tip is not an ancestor of `castle` into a `regate` action, which runs `court collect <ids> --regate --standup`, mints a brand-new convoy (fresh `the-gatehouse/<new-id>` worktree, one full unified suite, promotion) and returns the Quest to `READY_TO_RAZE`. The Night's Watch's own sync/rebase lane re-creates `court: sync ...` bookkeeping commits on the Quest branch whenever `castle` advances, so the branch tip is re-diverged on every tick and `raze` refuses again. The regate attempt counter is reset to 0 whenever the regate action itself exits 0, so the retry cap never trips and the loop is unbounded. On the pb-app board the four Quests `Q1380`, `Q1432`, `Q1455`, `Q1457` were re-gated through convoys `-586`, `-590`, `-592` (and again at 12:36Z) with a non-`.court`/`.kilo` delta of exactly zero every time.

## Goal & Scope

Make the Night's Watch structurally incapable of this failure mode. Two deliverables, both in this repository's engine (`court/`) with deterministic tests:

1. **Durable, cross-tick convoy admission.** Add an admission guard so that at most a bounded number of gatehouse convoys are integrating at any moment, independent of tick boundaries and of `ps`-census timing. The guard must consult durable state — the convoy lifecycle records under `.court/convoys/*.json`, or an explicit in-flight marker written when a convoy is collected and cleared when it is promoted or torn down — never a per-tick counter. A convoy counts as in-flight for the whole duration of its unified suite run, even though the suite's `pytest` processes never appear as `kilo run` sessions. The bound must be configurable and must default to a value that keeps the host safe at today's suite size.
   - `--max-workers` (or a new dedicated cap, e.g. `--max-convoys`) must genuinely bound concurrent convoys.
   - `court nightwatch --report` and `--status` must show the bound and the current in-flight convoy count.
2. **Terminate the zero-delta regate livelock.** A `READY_TO_RAZE` Quest whose branch carries no production delta versus `castle` (its unique commits are all Court bookkeeping — `court: ...` subjects and/or `.court/**`, `.kilo/**` paths only) must never be sent back through a fresh convoy. It must instead be razed (realign the branch tip to `castle` as a content no-op, per the existing realign precedent, then raze) or parked for a Steward with a specific, non-repeating escalation note.
   - `court raze`'s ancestor gate must tolerate a branch whose only post-promotion divergence from its recorded `cogship_promoted_commit` is Court bookkeeping (same defect family as the `unpromoted_quests_in_trunk` bookkeeping-tip false positive).
   - The regate attempt counter must count *completed cycles* — a Quest that returns to a raze-refusing `READY_TO_RAZE` after a regate — so the retry cap actually bounds the loop and escalates to a Steward.

## Constraints

- Engine-only change confined to `court/` (plus `tests/`). Do not modify pb-app, and do not add or change production app code.
- Add deterministic unit tests for both defects: drive `plan()` as a pure function for the admission and routing logic, and use a real temporary git repository for the raze/realign path.
- Include a regression test that replays the observed incident state — four in-flight convoy records (or four live gatekeeper sessions plus their suites) — and asserts the planner admits no further convoy and routes no zero-delta Quest to regate.
- Preserve existing behaviour: spawn budgets, per-Quest cooldowns, cohort batching, `collect_min_batch`/`collect_batch_window`/`collect_batch_max`, and the one-convoy-per-tick rule must not regress.
- New policy knobs must have safe defaults that match today's intent, so existing launchers keep working without edits.
- The unified suite must pass: `python3 -m pytest tests -q`.

M'Lord's Charter Notes: Chartered by the Steward 2026-11-13T12:5xZ from the 12:42Z thrash incident report. Both defects are in court/nightwatch.py and are fixed together because a fix to either alone leaves the host exposed: Defect A alone leaves the zero-delta loop running (merely throttled), and Defect B alone leaves convoy admission unbounded if any future cycle re-appears. Priority: Defect B is the live loop breaker; Defect A is the containment invariant. Dispatch held until the host finishes shedding load.


## Policy directive (M'Lord, 2026-11-13T13:20Z) — sequential convoys, one ship at a time

The 2026-11-13T12:42Z four-convoy thrash shows the missing invariant precisely, and it is not the one first assumed. `court collect` **already** refuses a second pack while a convoy is live — the *Single-convoy guard* in `cmd_collect`. But "live" is decided by `session_is_fresh(gatehouse_worktree)`: *a Gatekeeper session file updated within 30 minutes*. A convoy whose Gatekeeper goes quiet for more than 30 minutes during a long unified suite is therefore declared dead, the guard stops blocking, and the next tick packs another convoy; the same heuristic lets the watch's `teardown` lane remove a live convoy's worktree. Admission is a **session-freshness heuristic**, not durable state.

M'Lord's directive:

- **One ship at a time.** Exactly one gatehouse convoy may be in flight at any moment by default (a configurable `--max-convoys`, default 1). The next convoy must not begin packing until the one ahead of it has finished packing — promoted/confirmed (or explicitly released) and its gatehouse released.
- **In-flight is durable, never a session heuristic.** A convoy is in-flight from the moment it is packed until it is promoted, torn down, or explicitly released. Record it in durable state (`.court/convoys/<id>.json` and/or an explicit marker) and derive the admission decision from that — not from session freshness, and not from a per-tick counter.
- **Configurable floor and max.** The cohort floor (`collect_min_batch`) is configurable and may be dynamic; the per-convoy max (`collect_batch_max`) defaults to 5.
- **`regate` obeys the same admission.** A regate must not mint a convoy while another is in flight, and must not bypass the cohort floor.
- **Feed the pipeline.** No single lane may monopolise the spawn budget: the `coin` lane (the TRIBUTE_READY backlog) and the `collect` lane must each make progress. On 2026-11-13 the regate lane consumed the single per-tick spawn slot while 19 GATE and 14 TRIBUTE_READY Quests waited.

Because packing is serialized, a cohort naturally accumulates GATE Quests while the current convoy integrates — which is what keeps pack size healthy without an artificially high floor.

## Expected Tribute

- [ ] **Admission (Defect A)** — a durable, cross-tick convoy admission guard exists: while any gatehouse convoy is in-flight (collected, suite running, not yet promoted or torn down), the planner admits no second convoy. Unit test: N successive ticks with one in-flight convoy produce zero additional convoy collect/regate actions.
- [ ] **Admission is genuinely bounded** — the in-flight count is derived from durable state (`.court/convoys/*.json` and/or an explicit marker), not from the `ps` census and not from a per-tick counter. Test proves admission holds when the Gatekeeper session is absent from the process census (the async-spawn and `pytest`-only window).
- [ ] **Admission is observable** — `court nightwatch --report` / `--status` prints the configured convoy bound and the current in-flight convoy count.
- [ ] **Livelock (Defect B)** — a `READY_TO_RAZE` Quest whose branch delta versus `castle` is bookkeeping-only is never routed to `regate`. Test covers both outcomes: razed via realign, or parked for a Steward with a specific note.
- [ ] **`court raze` bookkeeping tolerance** — real-temp-repo test: a Quest whose recorded promoted commit is an ancestor of `castle` and whose branch tip carries only trailing `court: ...` commits razes successfully after the documented content-no-op realign; a branch with genuine production-tip divergence is still refused.
- [ ] **Retry cap terminates** — a simulated raze-refuses, regate, raze-refuses cycle increments the attempt counter and escalates to a Steward after the cap instead of looping forever.
- [ ] **Incident replay** — a regression test reconstructs the 2026-11-13T12:42Z state (four in-flight convoys / four live gatekeeper sessions) and asserts the planner admits no further convoy and re-gates no zero-delta Quest.
- [ ] **Suite green** — `python3 -m pytest tests -q` passes with the new tests included; no existing Night's Watch test regresses.
- [ ] **Commutation (named here, performed outside this Quest)** — (a) the fix is vendored into pb-app's engine copy `.court/engine/`; (b) the pb-app Night's Watch is restarted so the live zero-delta loop stops; (c) the four affected pb-app Quests (`Q1380`, `Q1432`, `Q1455`, `Q1457`) are realigned and razed; (d) any new cap is set in pb-app's `.kilo/watch/run-nightwatch.sh`.

- [ ] **Self-advance (do this LAST)**: once Tribute is rendered and `git rev-list --count HEAD..castle` is 0, run `python3 -m court.cli advance Q002-Castle-Nightwatch-Convoy-Backpressure TRIBUTE_READY` yourself. Nothing else flips the status out of WORKING.

## Tribute Rendered

## Master of Coin's Audit

## Judgement of the Condemned

## Cogship Log

## Audience Log
