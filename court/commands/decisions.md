---
description: List every decision awaiting M'Lord (the Audience frontier), with a decree-packet skeleton per item
agent: steward
---
Arguments: $ARGUMENTS

Run `python3 -m court.cli decisions` (add `--json` for tooling). This is the
deterministic frontier: `HELD`/`PUNISHED` quests, Serf Audiences pending in
`TRIBUTE_READY`/`GATE`, and outstanding UI sign-offs. Do not manufacture a
decision when the command prints "No decisions are waiting".

Present each item plainly:

- the Quest and its stage,
- the question or blocker (verbatim from the charter where present),
- what a ruling unblocks,
- your recommendation and the consequence of each option.

When M'Lord rules, record it as a decree packet — locally with
`python3 -m court.cli decree --quest <id> --ruling "<ruling>" [--status WORKING]`,
or, when M'Lord is away on voice, by adding `.court/decrees/<id>.json` to the
`pleasance` branch (the schema is printed by `court decisions`). A `HELD` quest
resumes at `WORKING` when the ruling leaves the status implicit. Razing,
shipping, and the pillory are NOT decree targets.
