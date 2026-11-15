---
description: Roll up where the Night's Watch is stuck and set up the manual work it cannot do
agent: steward
---
Arguments: $ARGUMENTS

Night's Watch triage:
1. `python3 -m court.cli nightwatch --report --repo <repo>` — a read-only digest from the journal + state: **livelock collects** that packed nothing, **standing escalations** (stale gatekeeper stamps, PENDING UI review), **goad/coin budget** usage (`serfs x goads_per_serf` before a Quest bubbles up), **failed actions**, **steward musters**, and **snapshot failures/restarts**.
2. `python3 -m court.cli nightwatch --status --repo <repo>` — daemon presence, last tick, per-Quest counters, and the 24-hour Steward muster tally.
3. Act on what the watch cannot: summon a Court Artist (`/artist`, `/atelier`) for PENDING UI review, regate or clear a stale gatekeeper stamp, and triage anything that reached its worker-recovery budget. With `--enable ...,atelier` the watch also **feeds the standing atelier itself**: a UI-review-pending GATE Quest is merged into the ONE long-lived studio automatically (`court atelier --standing <ids>`) when no artist turn is live — a local git merge + server ensure, never a reasoning worker, so it costs no worker slot. A dead gatehouse worktree (a convoy that died mid-integration) is cleared automatically: teardown is on by default, and a teardown that fails twice is escalated with the exact manual `court fork-teardown-list` / `git worktree remove` commands. The watch never improvises work; it only rolls the mechanical verbs under hard caps.

4. Clogs are usually drift, not missing intent: `python3 -m court.cli enqueue <quest> --why "..."` derives the action that can advance the Quest (dispatch/goad/coin/collect) and the watch drains it under its worker ceiling; `--brief <repo-local file>` carries the actual remediation instruction into a dispatch/goad so the worker is not re-reading a charter it already satisfied. Chain follow-ons with status edges (`court vigil add ... --when quest-status:<quest>=TRIBUTE_READY --then coin:<quest>`), never with repeated manual goads; `--then rebase:<quest>` converges a tree with no model turn at all.

Opt-in last resort — steward muster:
- `court nightwatch --enable goad,coin,collect,dispatch,steward` lets the watch summon ONE single-turn Steward triage session when the board has been unchanged for 45m, nothing is running anywhere, and a standing blocker remains. It is cooldown-capped (6h) and daily-capped (2), and it never re-prompts.
- Test the prompt without spawning: `python3 -m court.cli muster --why "manual check" --print-prompt`.
