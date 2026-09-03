---
description: Aggregate Serf penance to identify tech debt, shortcuts taken, and opportunities to improve system rules/prompts
agent: steward
---
Arguments: $ARGUMENTS

Follow the Atone Protocol:
1. Run `court rollup --section penance $ARGUMENTS` to extract all Penance self-flagellations.
2. Synthesize the findings into two actionable categories:
   - **Technical Debt & Deferred Items**: Concrete shortcuts or missed edge cases to queue into the project backlog.
   - **System & Prompt Improvements**: Recurring agent confusion, tooling failures, or ambiguous rules to fix in `.court/templates/`, `.kilo/prompts/`, or repo guidelines.
3. Present the remediation plan to M'Lord.
