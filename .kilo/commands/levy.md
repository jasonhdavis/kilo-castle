---
description: Deterministic worktree triage, live task progress sync, orphan detection, and the one mandatory Audience checkpoint for the one-shot audit
agent: steward
---
Arguments: $ARGUMENTS

Follow the Levy Protocol:

1. **Pre-Flight Sweep, Agent Manager Idle Inspection & Deterministic Triage**:
   - Before triaging, surface orphaned worktrees (live in Agent Manager with no matching active Quest) via `court status`'s Orphaned Worktrees block and close them out (`court raze <id>` / `court archive <id>`) or raise them with M'Lord.
   - Call `agent_manager` (`action: "list"`) to cross-reference live session activity (`idle` vs `busy`) with Quests in `WORKING` (Working) phase.
   - Run single-shot levy CLI command:
     ```bash
     python3 -m court.cli levy $ARGUMENTS
     ```
    - This deterministically syncs tribute checklists, audits base drift, calculates task progress, and categorizes Quests into:
      - 🪙 **Levied & Ready for Coin Audit** (`TRIBUTE_READY` / Tribute Ready)
      - ⚔️ **Active Questing Worktrees** (`QUESTING` / Questing, showing live `[Tasks: X/Y (%)]`, flagging idle Serfs)
      - 🔴 **Non-Compliant / Drifted / Blocked** (with exact failure reasons)
    - **Note (Q149, 2026-09-05):** this command now runs a mechanical `git merge castle` sweep itself, per-quest, immediately before each quest's own audit/advance decision — zero agent turns. It auto-resolves the two routine conflict shapes (foreign Quests' ledger files -> take castle's side; a quest's own History-table-only divergence -> union/max) and only ever reports a genuine conflict (real `status:` disagreement, real code conflict) for manual attention. Never spawn a Serf agent purely to run `git merge castle` — that round-trip is exactly what let drift outgrow the queue before this fix; run `python3 -m court.cli rebase <id> --dry-run` first to check whether an agent is actually needed at all.

2. **Route QUESTING Quests: Diagnose the Signature Yourself, Then Choose Master of Coin or `/goad` (Never Both, Never Neither)**:
    - **For every Quest in `QUESTING`, read its own `court levy` signature before deciding anything — do not skip straight to `/goad` by habit.** The signature is already sitting in Step 1's output: task-checklist percentage, `CLEAN`/dirty working tree, and drift (`↑`/`↓`). It splits into exactly two cases:
      - **Case A — Serf-complete / paperwork-only signature: `[Tasks: X/Y (100%)] [CLEAN] ↑0 ↓0`.** This means the actual coding is done — only the paperwork (`# Tribute Rendered`, task-file checkbox sync, the self-advance to `TRIBUTE_READY`) is missing, exactly what a Serf whose session already exited without writing up Tribute looks like. **Do not run `/goad` for this signature.** Goading would just re-prompt (or re-spawn) the Serf purely to write paperwork it was already neglectful about — a wasted round-trip, when the Master of Coin is *already* explicitly chartered with "broad authority" to write/repair exactly that paperwork itself and to run live verification confirming the work is real (`.court/templates/master_of_coin_review_prompt.md` Authorization Boundaries item 2). Instead, dispatch the Master of Coin directly and immediately, right here in Step 2:
        - Start a **brand-new, dedicated** Agent Manager session bound to the Quest's existing branch/worktree (`agent_manager` `start`, `mode: "worktree"`, `branchName` set to the Quest's canonical branch, model **Gemini 3.8 Flash** — `openrouter/google/gemini-3.8-flash`), using `.court/templates/master_of_coin_review_prompt.md` as its initial prompt. This is the exact same dispatch pattern Step 4 uses for Quests that reach `TRIBUTE_READY` on their own — it is simply being triggered a step earlier here, directly from `QUESTING`, instead of waiting on a Serf to self-advance first.
        - Record metadata the same way Step 4 does: `python3 -m court.cli set-field <id> master_of_coin_session_id <session_id>` / `master_of_coin_model "openrouter/google/gemini-3.8-flash"`.
        - The Master of Coin verifies the work is real, writes the full `# Tribute Rendered` section, syncs task-file checkboxes, and renders its one Pass/Fail verdict itself — Pass advances straight to `GATE` (silent, no Audience interrupt needed, per Step 4's Pass outcome), Fail pillories with Decrees for a successor. Either outcome fully resolves this Quest for the current levy pass: **do not also run `/goad`, and do not also run a second, separate Master of Coin dispatch on it later in Step 4** — that would be double-dispatching the same accounting work.
      - **Case B — genuinely incomplete signature: checklist below 100%, and/or a dirty/outstanding working tree.** Real coding work remains, which only a Serf (never the Master of Coin, whose remit forbids touching the diff) can finish. **Run `/goad <quest_id>`** as before — this is now `/goad`'s sole reason for existing inside `/levy`. Do **not** goad purely to clear drift — Step 1 already did that mechanically; `/goad` itself re-checks drift too, that's not the reason to invoke it.
    - **`/goad` must always end in either a live session response or an explicit escalation** — per the updated `.kilo/commands/goad.md`, if `agent_manager list` shows no session at all for a `QUESTING` Quest's worktree (common once a Serf's terminal has exited after finishing its checklist), `/goad` starts a brand-new session bound to that same existing branch (`GLM-5.3-Flash` / `openrouter`) with `.court/templates/goad_prompt.md` as its initial prompt, rather than reporting the quest as "in progress as expected" with nobody actually working it. (In practice this fallback should now mostly land on genuine Case B quests, since Case A is intercepted above before `/goad` is ever called — but `.court/templates/goad_prompt.md`'s own Case A branch remains as a safety net if a Serf session unexpectedly responds to a Case B goad with a now-Case-A state, or if `/goad` is invoked manually/directly outside of `/levy`.)
    - **For Idle Serfs with Incomplete Tribute in Questing**: Prompt the existing session by default using `.court/templates/bear_tribute_prompt.md`. Only dismiss and dispatch a fresh Serf into the same branch if stalled.
    - **For Active Questing Serfs**: Leave in `QUESTING` without interruption.

3. **The Mandatory Audience Checkpoint (Complete Tribute)**:
   - For Quests in `TRIBUTE_READY` (Tribute Ready), present M'Lord with the **required per-Quest summary structure** (the one Audience checkpoint before summoning Master of Coin):
     - **Verdict line**: ✅ (Approved), ⚠️ (Caution), or 🔴 (Blocked).
     - **2-4 sentence bullets**: Synthesizing what shipped (architecture/deliverables, not a raw diff dump).
     - **Penance bullet**: Only if something was shortchanged, deferred, or technical debt incurred.
     - **Logical grouping**: Grouped under logical section headers (app/theme/Epic), not a flat Quest-ID table.
   - Get explicit go/no-go from M'Lord.
   - **Shortcut, not a separate command**: `python3 -m court.cli levy <quest_id>` already scopes
     everything above to one Quest — it's a strict superset of a bare audit (same underlying
     `ward.audit_quest()` call, plus the rebase/sync/advance steps), so there is no separate
     single-Quest command to reach for. If M'Lord directly names a specific Quest and asks to push it
     through right now, that request *is* the go/no-go — present the summary above inline for the
     record, then continue straight into Step 4 without waiting on a second confirmation. Never extend
     this shortcut to any other Quest in the batch that M'Lord didn't explicitly name.

4. **The Continuous Summon Chain (Levy → Coin → Artist → Gatekeeper)**:
   - Upon M'Lord's assent, immediately dispatch the **Master of Coin** (`.court/templates/master_of_coin_review_prompt.md`, model **Gemini 3.8 Flash** — `openrouter/google/gemini-3.8-flash`, one tier above the Serf's GLM 5.3 Flash) into a **brand-new, dedicated** Agent Manager session bound to the Quest's existing branch/worktree (`agent_manager` `start`, `mode: "worktree"`, `branchName` set to the Quest's canonical branch). **Never** prompt/reuse the Serf's own `serf_session_id` for this.
   - Record metadata:
     ```bash
     python3 -m court.cli set-field <id> master_of_coin_session_id <session_id>
     python3 -m court.cli set-field <id> master_of_coin_model "openrouter/google/gemini-3.8-flash"
     ```
   - The Master of Coin has broad authority to run live verification commands in the worktree (no test suites) and to complete the Serf's paperwork (Ballad/Tally/Penance/Audience/Humble-Opinion headings, task-file checkbox sync), but a narrow remit — no code fixes, no serf work. It renders exactly one Pass/Fail verdict auditing expected vs. delivered scope, unrequested extra tribute / scope smuggling (especially unsolicited UI alterations on non-UI tasks), duplication, and Neon compute costs, plus Recommended Next Steps, a `Commutation` note (what production still needs to do to activate this Tribute's value), and a `UI Review` verdict.
   - **Post-Audit Routing on Pass**:
     - **If UI deliverables exist (UI Review: PENDING)**: The Steward's automatic / recommended action before collection is to summon the **Court Artist** (`/artist <id>`). While the Serf creates the initial UI, M'Lord and the Court Artist refine the UI/UX directly in the studio with the active worktree runserver. Once M'Lord approves and the Court Artist signs off and commits, proceed to `/collect`.
     - **If headless / backend-only (UI Review: None required / Approved)**: Pass seamlessly triggers the next link in the summon chain: advancing to `GATE` and invoking `/collect` (Gatekeeper).
   - **On Fail**: Fail has exactly one outcome: the Quest is pilloried (`/pillory`) with Decrees — the Steward charters a successor from them.

5. **Report to M'Lord & Recommended Next Command**:
   - Present the concise triage summary rendered with `status_label()` names.
   - Append a **Recommended next command(s)** line computed from live state (e.g. idle complete tribute → `/levy`, review approved → `/collect`, ready to raze → `/raze`, teardown-list).
