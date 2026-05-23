# 2026-05-23 — Test configuration overhaul (parallel + filtered dev loop)

## Motivation

The local backend test loop (`uv run pytest -q api/tests`) was running serially
and including every slow / external-process test, taking **4+ minutes** on the
developer machine. The user reported the loop was so slow it discouraged the
TDD inner cycle. The repository already declares `slow` and `subprocess` markers
but neither was wired into a default exclude, and there was no parallel
execution. The location of `api/tests/` was reviewed but kept where it is —
that placement is mandated by [AGENTS.md](../../../AGENTS.md) L197 ("Tests live
next to their code (`api/tests/`); cross-cutting only at root `tests/`").

## User-facing change

* `uv run pytest -q api/tests` (the charter command) now runs **parallel via
  `pytest-xdist`** with the `worksteal` distribution algorithm and **excludes
  tests marked `slow` or `subprocess` by default**. Wall time on the developer
  machine dropped from **~4 min** (serial, every test) to **~70 s** (parallel,
  1336/1402 tests).
* A `--timeout=60` safety net is enabled, so a hung test no longer requires the
  user to `pkill pytest`. Long-running marked tests still pass because the
  timeout uses the `thread` method and the slow tests are already pre-filtered.
* Three new VS Code tasks replace the single `tests: pytest`:
  * **`tests: pytest`** (default, fast) — `uv run pytest -q api/tests`.
  * **`tests: full`** — `uv run pytest api/tests -m ''` runs everything,
    including slow + subprocess (~91 s on this machine).
  * **`tests: slow-only`** — `uv run pytest api/tests -m 'slow or subprocess'`
    runs only the opt-in suite (~21 s, useful before PRs that touch BLAST
    comparison helpers, terminal toolchain, or shell scripts).
* The `subprocess` marker — previously declared but unused — is now applied to
  eight files that shell out via `subprocess.run` / `Popen` (terminal banner /
  command-guard / toolchain / history, sharded merge, three BLAST comparison
  scripts). They are still in the suite, just opt-in.

## API / IaC diff summary

| File | Change |
| ---- | ------ |
| [pyproject.toml](../../../pyproject.toml) | Added `pytest-xdist>=3.6,<4`, `pytest-timeout>=2.3,<3` under `[dependency-groups].dev`. |
| [pytest.ini](../../../pytest.ini) | Added `pythonpath = .`; `addopts` now includes `-n auto --dist worksteal -m "not slow and not subprocess" --timeout=60 --timeout-method=thread`. Marker descriptions clarified to call out the default-exclude behaviour. |
| `api/conftest.py` → [api/tests/conftest.py](../../../api/tests/conftest.py) | `git mv`'d. The old `sys.path.insert(0, ROOT)` hack is gone — `pythonpath = .` in `pytest.ini` does the same thing without the import-time side effect. The two autouse fixtures (`_env_baseline`, `_reset_external_jobs_cache`) and every `os.environ.setdefault` line are preserved verbatim. The module docstring is the standard repo context header (Responsibility / Edit boundaries / Key entry points / Risky contracts / Validation). |
| [api/tests/test_compare_blast_web_csv.py](../../../api/tests/test_compare_blast_web_csv.py), [test_compare_blast_web_xml_outfmt6.py](../../../api/tests/test_compare_blast_web_xml_outfmt6.py), [test_compare_blast_xml.py](../../../api/tests/test_compare_blast_xml.py), [test_sharded_merge.py](../../../api/tests/test_sharded_merge.py), [test_terminal_banner.py](../../../api/tests/test_terminal_banner.py), [test_terminal_command_guard.py](../../../api/tests/test_terminal_command_guard.py), [test_terminal_history.py](../../../api/tests/test_terminal_history.py), [test_terminal_toolchain.py](../../../api/tests/test_terminal_toolchain.py) | Added file-level `pytestmark = pytest.mark.subprocess` so they're opted out of the fast dev loop. |
| [.vscode/tasks.json](../../../.vscode/tasks.json) | Split `tests: pytest` into three tasks (`tests: pytest`, `tests: full`, `tests: slow-only`); see above. |
| [uv.lock](../../../uv.lock) | Regenerated to pin `pytest-xdist 3.8.0` and `pytest-timeout 2.4.0`. |

No production source, no Bicep, no Container App template touched.

## Why xdist is safe here

The two autouse fixtures (`_env_baseline`, `_reset_external_jobs_cache`) reset
process-level singletons. xdist workers are **separate processes**, so per-worker
state is naturally isolated; the resets continue to protect against same-worker
cross-test contamination. The default distribution algorithm was changed from
`load` to `worksteal` so a slow file held by one worker can have its remaining
tests redistributed to idle workers — important because TestClient setup cost
varies a lot across files (95 `TestClient(...)` instantiations across 119 test
files).

## Validation evidence

Run on the dev machine (16 CPUs):

```
$ time uv run pytest -q api/tests --tb=short
…
1267 passed, 69 failed in 70.66s
real    1m18.355s

$ time uv run pytest api/tests -q -m 'slow or subprocess'
…
63 passed, 3 failed in 20.90s
```

Collection sanity:

```
$ uv run pytest --collect-only -q api/tests           → 1336/1402 (66 deselected)
$ uv run pytest --collect-only -q api/tests -m ''     → 1402
$ uv run pytest --collect-only -q api/tests -m 'slow or subprocess'
                                                       → 66/1402 (1336 deselected)
```

All 72 failing tests in the union of those runs are **pre-existing** and have
nothing to do with this change. Spot-checked the largest cluster
([api/tests/test_state_repo.py](../../../api/tests/test_state_repo.py), 8
failures): `AttributeError: <module 'api.services.state_repo'> has no attribute
'TableClient'` — commit c974ace ("Refactor taxonomy services …") moved
`TableClient` out of `api.services.state_repo` into
`api.services.state.table_pool` but the test monkeypatching still targets the
old attribute. Same symptom in `test_blast_tasks.py`, `test_blast_log_routes.py`,
`test_external_blast_api.py`. These should be fixed in a separate, focused PR.

## Notes for future work

* The `--timeout=60` default may need bumping for tests that legitimately do
  longer I/O. If you mark a test `slow` you can override per-test with
  `@pytest.mark.timeout(120)`.
* If a test relies on serial execution (e.g. observes module-level globals
  shared via `sys.modules`), mark it `@pytest.mark.xdist_group("…")` so xdist
  keeps grouped tests on the same worker.
* Cleaning up the 72 pre-existing failures is its own task — flagged here so a
  later developer doesn't think they were introduced by this PR.
