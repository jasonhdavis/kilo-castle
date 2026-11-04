---
description: Deterministic successor audit of PUNISHED quests — reads real charters, flags no-successor and not-advanced successors, writes docs/PUNISHED_AUDIT.md
agent: steward
---
Arguments: $ARGUMENTS

Audit the PUNISHED quests on the board without inventing any work:

`python3 -m court.cli punished-audit [--repo <repo>] [--out <dir>] [--json]`

1. Reads the REAL charters under `.court/quests/*.md`: the `Judgement of the
   Condemned` section (Reason / Successor Quest lines) plus the frontmatter
   `pilloried_by` / `pillory_of` links (kept linked by `court charter
   --pillory-of` / `court pillory --successor`).
2. Classifies each punished quest:
   - (a) **No successor chartered** — flag.
   - (b) **Successor chartered but not advanced** — successor status is still
     OPEN / PLANNED / CHARTERED / HELD / DEMOTED (not yet dispatched).
   - Chain: the successor is ITSELF PUNISHED.
3. Writes `docs/PUNISHED_AUDIT.md` (report) and prints the summary counts;
   `--json` prints the full structured audit instead of the file.
4. Report-only: creates no quests, changes no charters.