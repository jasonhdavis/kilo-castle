# 📜 The Plot: {{ quest_id }} — {{ quest_title }}

M'Lord, here is the realm as I now understand you intend it.

## 👑 Intent
{{ intent }}

## ⚜️ Decrees
- {{ decree_1 }}
- {{ decree_2 }}

## 🧱 Bounds of the Realm
- **In Scope**:
  - {{ in_scope_1 }}
  - {{ in_scope_2 }}
- **Beyond the Walls**:
  - {{ out_of_scope_1 }}
  - {{ out_of_scope_2 }}

## 🗺️ Findings
- {{ finding_1 }}
- {{ finding_2 }}

## ⚖️ Consequences & Architectural Decisions
- {{ consequence_1 }}

## ⏳ Delayed Judgments
- **Adjourned**: {{ delayed_matter }}
  - **Cause**: {{ reason_for_deferral }}
  - **Returns To**: {{ future_milestone_or_quest }}
  - **Bars Dispatch**: false

## 🏁 Victory (Expected Tribute Checklist)
- [ ] {{ acceptance_criterion_1 }}
- [ ] {{ acceptance_criterion_2 }}
- [ ] {{ acceptance_criterion_3 }}
- [ ] Full scoped test suite passes cleanly.
- [ ] Report to the King / Bear Tribute submitted strictly conforming to 5-part contract (Ballad, Tribute with Tally, Penance, Audience, Humble Opinion).

---

The Tree is exhausted.
Shall I confirm the Plot and commission the Quest Remit into `PLANNED`?

## 🌲 Scout Close-Out (automatic on sealing)

If this Plot was sourced from a Scout Report (a `kind: scout` Quest), sealing it with
M'Lord's assent **automatically closes out the source Scout as a natural side effect** —
clearing a scout tree is part of running `/plot` on its findings, never a separate manual
step:
1. Advance the Scout Quest to `READY_FOR_TEARDOWN`:
   `python3 .court/engine/cli.py advance <scout_id> READY_FOR_TEARDOWN --note "Plot sealed from Scout Report; scout tree cleared"`
2. Log the lineage in the new Quest's record (note the source Scout in the new Quest's
   Goal & Scope).
3. Queue the Scout's worktree for Ashes (`court raze <scout_id>` verifies and aligns;
   M'Lord prunes it manually from Ashes).

Scouts never merge their spike work wholesale — the reusable pieces are rebuilt as proper
Quests (see `AGENTS.md`'s Investigation lane).
