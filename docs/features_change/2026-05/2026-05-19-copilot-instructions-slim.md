# 2026-05-19 — Slim Copilot charter; extract detail to `docs/copilot/`

## Motivation

`/.github/copilot-instructions.md` had grown to 356 lines / ~31 KB and
`AGENTS.md` to ~10 KB. Both are loaded into the Copilot prompt for *every*
request in this workspace. Combined with persistent user-memory notes that
also enter the prefill, the always-on payload had reached ~41 KB, which
visibly slowed first-token latency on Claude Sonnet 4.6 (1M context) sessions.

The detail sections (repository tree, browser-terminal lifecycle, Celery
resource-plane table, dashboard card spec, glassmorphism CSS tokens) are only
needed when an agent is actively implementing in those areas. They do not
need to occupy the always-on context.

## User-facing change

None. This is a documentation reorganisation; runtime behaviour is unchanged
and no source code, infra, or tests were touched.

## What moved where

| Section (was) | New location |
|---|---|
| §4 Repository Layout + §15 "Where Things Live" | [docs/copilot/repo-layout.md](../../copilot/repo-layout.md) (also absorbs the backend / frontend / infra module maps that used to live in `AGENTS.md`) |
| §5 Authentication Flow | [docs/copilot/auth-flow.md](../../copilot/auth-flow.md) |
| §6 Browser Terminal — Sidecar Lifecycle | [docs/copilot/browser-terminal.md](../../copilot/browser-terminal.md) |
| §7 ElasticBLAST Resource Plane (Celery task table) | [docs/copilot/resource-plane.md](../../copilot/resource-plane.md) |
| §8 Monitoring UI (dashboard card spec) | [docs/copilot/monitoring-ui.md](../../copilot/monitoring-ui.md) |
| §10 Glassmorphic UI CSS tokens / `.glass-card` template | [docs/copilot/glass-ui.md](../../copilot/glass-ui.md) |

The charter now keeps a one-line pointer to each (`Detail moved to docs/copilot/…`).

## What stayed in the always-on charter

All NON-NEGOTIABLE and safety-critical content:

- §0 Implementation Discipline
- §1 Mission (unchanged short version)
- §2 Language Policy
- §3 Stack & Versions (unchanged decision table; needed for every PR)
- §9 Storage Network Isolation (HARD REQUIREMENT, unchanged)
- §11 Coding Standards (uv-only, Never Run Command, `terminal_exec` contract)
- §12 Security Checklist
- §13 Process Discipline (features_change + validation triplet)
- §14 Out of Scope

## AGENTS.md

The module-map tree dumps (backend `api/`, frontend `web/src/`, `infra/`)
were moved into [docs/copilot/repo-layout.md](../../copilot/repo-layout.md)
and replaced with a single pointer. The TL;DR, "Where to read first" table,
backend route map, tripwires list, validation cheatsheet, and conventions
list were kept as-is — those are the high-signal navigation that future
sessions actually need on first load.

## Size diff (always-on payload)

| File | Before (lines / bytes) | After (lines / bytes) |
|---|---|---|
| `.github/copilot-instructions.md` | 356 / 30 815 | 201 / 18 348 |
| `AGENTS.md` | 208 / ~10 200 | 149 / 9 044 |
| **Total always-on** | **~41 KB** | **~27 KB** (-34%) |

New on-demand detail under `docs/copilot/`: 6 files, ~18 KB total. These are
only loaded when an agent explicitly reads them via `read_file`.

## Validation

- `grep -R "docs/copilot/" .github/ AGENTS.md` → all six pointers resolve to
  existing files (`auth-flow.md`, `browser-terminal.md`, `glass-ui.md`,
  `monitoring-ui.md`, `repo-layout.md`, `resource-plane.md`).
- No source / infra / test files modified, so no `pytest` / `npm build` /
  `azd what-if` evidence is required.
- Manual re-read of the slimmed charter confirms every NON-NEGOTIABLE rule
  and every Security Checklist item is still present.
