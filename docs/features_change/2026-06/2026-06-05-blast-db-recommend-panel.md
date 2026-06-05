---
title: BLAST database recommendation panel (R8 frontend)
description: Wire the existing database selection oracle into a "Help me choose" panel on the BLAST submit form.
tags:
  - user-guide
  - blast
---

# BLAST database recommendation panel ("Help me choose")

## Motivation

Issue #2 R8 ("Database selection oracle") shipped its backend in an earlier
change: `api/services/blast/db_recommendation.py` (`recommend_database`, versioned
rule table) and the route `GET /api/blast/databases/recommend`, both covered by
`api/tests/test_blast_db_recommendation.py`. The frontend, however, never called
the endpoint — the acceptance criterion "**Help me choose** panel on
`/blast/submit`" was unmet. A researcher who is unsure whether to pick `core_nt`,
`nt`, `nr`, `swissprot`, `refseq_rna`, … had no in-product guidance.

## User-facing change

The **Choose Search Set** step on `/blast/submit` now has a collapsed
"Help me choose a database" affordance. Expanding it lets the researcher pick a
search **Goal** (Identify / Find near-identical / Match transcripts / Match
genomes / Prefer curated / Maximum coverage) and an optional **Taxon** hint
(prefilled from the taxonomy filter step). Clicking **Recommend** calls the
oracle and renders:

- a **Recommended** database + an **Alternative**, each with a one-sentence
  rationale straight from the versioned rule table;
- a **Use `<db>`** button per suggestion that selects the database — but only
  when it is already downloaded and ready, otherwise the panel shows a
  "not downloaded yet — get it from the Dashboard" hint so the user is never
  handed a path that would silently block Submit;
- the oracle's contextual notes and the ruleset version.

The molecule (DNA vs protein) is inferred server-side from the selected BLAST
program, so the panel needs no extra molecule input.

## API / IaC diff summary

- No backend change. The route `GET /api/blast/databases/recommend` and the
  service rule table are unchanged.
- `web/src/api/blast.ts`: added `BlastDbSuggestion`, `BlastDbRecommendation`,
  `BlastRecommendGoal` types and the `blastApi.getDatabaseRecommendation()`
  client method (additive only).
- `web/src/pages/blastSubmit/DatabaseRecommendPanel.tsx`: new panel component.
- `web/src/pages/blastSubmit/DatabaseSection.tsx`: render the panel under the
  section header.
- `web/src/theme/glass.css`: `.blast-db-reco*` styles (calm navy/blue glass tones,
  consistent with the existing chips).

## Validation evidence

- `npx vitest run src/pages/blastSubmit/DatabaseRecommendPanel.test.ts` — 5 new
  tests pass (`readyPathForSuggestion` ready/in-flight/missing cases; `GOAL_OPTIONS`
  exactly mirrors the backend `SUPPORTED_GOALS` tuple).
- `cd web && npx vitest run` — full suite **675 passed**.
- `cd web && npm run build` — green.
- `uv run pytest -q api/tests/test_blast_db_recommendation.py` — 8 passed
  (backend contract unchanged).
- `uv run ruff check api/routes/blast/databases.py api/services/blast/db_recommendation.py`
  — clean.
