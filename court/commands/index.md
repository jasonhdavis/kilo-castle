---
description: Deterministic AI-orienting snapshot — branch tips, quest board, worktrees, punished+successors, bottlenecks (docs/AI_INDEX.md + .json)
agent: steward
---
Arguments: $ARGUMENTS

Generate a compact, deterministic repository-intelligence snapshot for AI agents
to read BEFORE exploring the repo:

`python3 -m court.cli index [--repo <repo>] [--out <dir>]`

1. Reads canonical `.court` records (`court status --json` live-state semantics),
   git branch tips for `main` / `castle` / `pleasance`, recent meaningful commits,
   registered worktrees, live kilo workers, PUNISHED→successor links, and the
   Night's Watch convoy state.
2. Emits `docs/AI_INDEX.md` (human/LLM-readable) plus `docs/AI_INDEX.json`
   (machine-readable) from the SAME deterministic snapshot.
3. The artifact is explicitly labelled **last known snapshot** — it carries its
   own `generated_at` timestamp and the `castle` HEAD SHA / trunk tips it was
   generated from. It is NOT live state: query `court status --json` (or
   `court schedule <id>` for per-quest eligibility) for runtime truth.
4. No secrets, no source dumping — only paths, tips, counts, and notable ids.

For an external agent, point it at the generated `docs/AI_INDEX.md` after
`PLEASANCE.md` (the lane contract). Regenerate whenever orientation starts to
drift.