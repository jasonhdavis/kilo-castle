---
description: List or present pending Audience decisions requiring M'Lord's judgment or authority
agent: steward
---
Arguments: $ARGUMENTS

Follow the Audience Protocol:
1. Scan `.court/quests/*.md` and `.court/LEDGER.md` for pending Audience requests.
2. If none, report that no decisions currently require M'Lord.
3. If pending decisions exist, present each as a structured block:
   - **The Decision Required**: One concise sentence.
   - **Context**: Essential background without chat dump.
   - **Options**: Clear mutually exclusive choices.
   - **Steward Recommendation**: The recommended path.
   - **Consequences**: Trade-offs for each option.
4. Record M'Lord's verdict in both `.court/LEDGER.md` and the Quest's `# Audience Log`.
