---
description: Extract and summarize production verification runbooks and UI paths across Quests
agent: steward
---
Arguments: $ARGUMENTS

Follow the Tally Protocol:
1. Run `python3 -m court.cli tally $ARGUMENTS` to deterministically extract all Tally sections across target Quests.
2. Synthesize the extracted verification instructions into a clear, actionable human QA and verification guide:
   - **Target Quest and Domain**
   - **Production and Staging UI Navigation Paths**: Exact URLs, routes, and query parameters.
   - **Click Paths and Interactive Workflows**: Step-by-step UI actions, form inputs, or dropdown selections.
   - **Example Commands and CLI Invocations**: Management commands or scripts with literal arguments.
   - **Expected Visual and Behavioral Outcomes**: Badges, status changes, table columns, metrics, or alerts to observe to verify correct implementation.
3. Present the structured verification guide to M'Lord so changes can be tested directly on live or staging environments with zero guesswork.
