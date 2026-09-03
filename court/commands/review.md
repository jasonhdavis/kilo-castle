---
description: Dispatch Master of Coin to review rendered Tribute on the Quest worktree
agent: steward
---
Quest: $ARGUMENTS

Follow the Review Protocol:
1. Confirm the Quest is at `REVIEW` or has a rendered Tribute in `# Tribute Rendered`.
2. Dispatch the **Master of Coin** (`.court/templates/master_of_coin_review_prompt.md`) directly onto the Quest's worktree.
3. Record `master_of_coin_session_id` and `master_of_coin_model`.
4. Await verdict:
   - If approved: advance to `GATE`.
   - If rejected: return to `WORKING` with specific deficiencies logged.
