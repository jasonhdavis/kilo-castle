---
description: Dispatch the Master of Coin to audit value, criteria, and compute costs of a rendered Tribute
agent: steward
---
Quest: $ARGUMENTS

Follow `.kilo/prompts/steward.md` §"6. Levy & Review (`/levy`, `/review`)":
1. Confirm the Quest is at `REVIEW` with a rendered Tribute
   (`python3 .court/engine/cli.py show <id>`). If the Tribute is missing or
   clearly incomplete, do not dispatch the Master of Coin — send it back to the
   Serf instead (use `/return`).
2. Dispatch the **Master of Coin** using the filled-in
   `.court/templates/master_of_coin_review_prompt.md`.
   Default model: Gemini 3.7 Flash (`openrouter/google/gemini-3.7-flash`).
3. Record: `python3 .court/engine/cli.py set-field <id> master_of_coin_session_id <session_id>`
   and `python3 .court/engine/cli.py set-field <id> master_of_coin_model "openrouter/google/gemini-3.7-flash"`.
4. Report back to M'lord once the Master of Coin resolves (pass/fail/ambiguous).
   If approved, advance to `GATE` and proceed to `/gate` for Gatekeeper dispatch.
