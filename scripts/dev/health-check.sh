#!/usr/bin/env bash
# health-check.sh - non-stop module-by-module error sweep.
#
# Runs every static check that does NOT require AKS / Azure / Redis,
# captures each tier's output into .logs/health-check/<timestamp>/,
# and produces a per-module summary.md at the end.
#
# Each tier is isolated with `set +e` + `|| true` so a failure in one
# tier never blocks the rest; the whole sweep always completes.
#
# Usage:
#   scripts/dev/health-check.sh                # tiers 0-6 (smoke last)
#   scripts/dev/health-check.sh --no-smoke     # skip API smoke (no port 8085 needed)
#   scripts/dev/health-check.sh --no-web       # skip frontend tiers
#   scripts/dev/health-check.sh --no-mypy      # skip mypy (it's the slowest)
#   scripts/dev/health-check.sh --only ruff,pytest
#
# Exit code = number of FAILED tiers (0 = all green).

set -u
set -o pipefail

# ---------- args ----------
SKIP_SMOKE=0
SKIP_WEB=0
SKIP_MYPY=0
ONLY=""
for arg in "$@"; do
  case "$arg" in
    --no-smoke) SKIP_SMOKE=1 ;;
    --no-web)   SKIP_WEB=1 ;;
    --no-mypy)  SKIP_MYPY=1 ;;
    --only=*)   ONLY="${arg#--only=}" ;;
    --only)     shift; ONLY="${1:-}" ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
  esac
done

# ---------- setup ----------
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/../.." && pwd)
cd "$project_root" || exit 1

ts=$(date +%Y%m%d-%H%M%S)
out_dir="$project_root/.logs/health-check/$ts"
mkdir -p "$out_dir"
ln -sfn "$ts" "$project_root/.logs/health-check/latest"

# Make sure tier logs do not contain editor/terminal escape sequences
export TERM=dumb
export PY_COLORS=0
export FORCE_COLOR=0
export PYTHONIOENCODING=utf-8

results_tsv="$out_dir/results.tsv"
: > "$results_tsv"

# tiers we actually plan to run (for --only filtering)
should_run() {
  local name=$1
  if [[ -z "$ONLY" ]]; then return 0; fi
  IFS=',' read -ra wanted <<< "$ONLY"
  for w in "${wanted[@]}"; do
    [[ "$w" == "$name" ]] && return 0
  done
  return 1
}

# ---------- helpers ----------
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'; BLUE='\033[34m'; RESET='\033[0m'

step() { printf "${BLUE}==> %s${RESET}\n" "$*"; }
pass() { printf "${GREEN}    PASS${RESET} %s\n" "$*"; }
warn() { printf "${YELLOW}    WARN${RESET} %s\n" "$*"; }
fail() { printf "${RED}    FAIL${RESET} %s\n" "$*"; }

# record_result <tier> <status: PASS|FAIL|SKIP|WARN> <errors_count> <notes>
record_result() {
  printf "%s\t%s\t%s\t%s\n" "$1" "$2" "$3" "$4" >> "$results_tsv"
}

# Run a tier. Capture stdout+stderr to <out_dir>/<tier>.log.
# Returns 0 always (caller inspects status file).
run_tier() {
  local tier=$1
  shift
  step "$tier"
  if ! should_run "$tier"; then
    warn "skipped (not in --only list)"
    record_result "$tier" SKIP 0 "filtered by --only"
    return 0
  fi
  set +e
  ( "$@" ) > "$out_dir/$tier.log" 2>&1
  local rc=$?
  set +e
  echo "$rc" > "$out_dir/$tier.exit"
  return $rc
}

# ---------- tier 0: env ----------
tier_env() {
  echo "== uv ==";        command -v uv && uv --version || echo "MISSING uv"
  echo "== python ==";    command -v python3 && python3 --version || echo "MISSING python3"
  echo "== node ==";      command -v node && node --version || echo "MISSING node"
  echo "== npm ==";       command -v npm && npm --version || echo "MISSING npm"
  echo "== git ==";       command -v git && git --version || echo "MISSING git"
}

# ---------- tier 1: python syntax / import-time ----------
tier_compileall() {
  uv run python -m compileall -q api scripts/dev
}

# ---------- tier 2: ruff ----------
tier_ruff() {
  # JSON output so we can bucket errors by module in the summary.
  uv run ruff check api --output-format=json
}

# ---------- tier 3: mypy ----------
tier_mypy() {
  # mypy is configured in pyproject.toml (strict, files=["api"]).
  # Run it as-configured; do not pass extra flags that would override the config.
  uv run mypy --no-color-output
}

# ---------- tier 4: pytest ----------
tier_pytest() {
  # Run every test, never stop on first failure, surface collection errors
  # as failures rather than aborting the sweep.
  uv run pytest -q api/tests \
    --maxfail=0 \
    --continue-on-collection-errors \
    --tb=line \
    -rN \
    -p no:cacheprovider
}

# ---------- tier 5a: web typecheck ----------
tier_web_tsc() {
  cd "$project_root/web" || return 1
  if [[ ! -d node_modules ]]; then
    echo "(node_modules missing; running 'npm install --no-audit --no-fund --silent')"
    npm install --no-audit --no-fund --silent
  fi
  npx --no-install tsc --noEmit
}

# ---------- tier 5b: web lint ----------
tier_web_lint() {
  cd "$project_root/web" || return 1
  npm run lint --silent
}

# ---------- tier 5c: web build ----------
tier_web_build() {
  cd "$project_root/web" || return 1
  npm run build --silent
}

# ---------- tier 6: api smoke ----------
SMOKE_PID=""
cleanup_smoke() {
  if [[ -n "$SMOKE_PID" ]] && kill -0 "$SMOKE_PID" 2>/dev/null; then
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
  fi
}
trap cleanup_smoke EXIT

tier_smoke() {
  # Start uvicorn in the background with AUTH_DEV_BYPASS so /api/me works.
  local port=18099   # off-band to avoid colliding with 'api: start' on 8085
  export AUTH_DEV_BYPASS=true
  export PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}"
  (
    uv run uvicorn api.main:app --host 127.0.0.1 --port "$port" --log-level warning
  ) > "$out_dir/smoke.uvicorn.log" 2>&1 &
  SMOKE_PID=$!

  # Wait up to 30s for the port to answer.
  local up=0
  for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
      up=1; break
    fi
    sleep 0.5
  done
  if [[ "$up" != "1" ]]; then
    echo "uvicorn did not become ready on :$port within 30s"
    echo "--- uvicorn log tail ---"
    tail -n 50 "$out_dir/smoke.uvicorn.log" || true
    cleanup_smoke; SMOKE_PID=""
    return 1
  fi

  # Hit a representative set of routes; never abort on individual failure.
  local rc=0
  local routes=(
    "/api/health"
    "/api/me"
    "/openapi.json"
    "/api/monitor/aks?subscription_id=00000000-0000-0000-0000-000000000000&resource_group=rg-x"
    "/api/monitor/storage?subscription_id=00000000-0000-0000-0000-000000000000&resource_group=rg-x&account_name=stx"
    "/api/monitor/acr?subscription_id=00000000-0000-0000-0000-000000000000&resource_group=rg-x&registry_name=acrx"
  )
  for r in "${routes[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:$port$r" || echo "000")
    printf "%-4s  %s\n" "$code" "$r"
    case "$code" in
      2*|401|403|404|410|503) : ;;   # endpoint is alive; auth/state mismatches are fine here
      *) rc=1 ;;
    esac
  done

  cleanup_smoke
  SMOKE_PID=""
  return $rc
}

# ---------- run all tiers ----------
declare -A TIER_STATUS

tiers=(env compileall ruff)
[[ "$SKIP_MYPY" -eq 0 ]] && tiers+=(mypy)
tiers+=(pytest)
if [[ "$SKIP_WEB" -eq 0 ]]; then
  tiers+=(web-tsc web-lint web-build)
fi
[[ "$SKIP_SMOKE" -eq 0 ]] && tiers+=(smoke)

run_one() {
  local t=$1
  case "$t" in
    env)         run_tier env         tier_env ;;
    compileall)  run_tier compileall  tier_compileall ;;
    ruff)        run_tier ruff        tier_ruff ;;
    mypy)        run_tier mypy        tier_mypy ;;
    pytest)      run_tier pytest      tier_pytest ;;
    web-tsc)     run_tier web-tsc     tier_web_tsc ;;
    web-lint)    run_tier web-lint    tier_web_lint ;;
    web-build)   run_tier web-build   tier_web_build ;;
    smoke)       run_tier smoke       tier_smoke ;;
  esac
  local rc=$?
  if [[ $rc -eq 0 ]]; then
    pass "$t"
    TIER_STATUS[$t]=PASS
  else
    fail "$t (exit=$rc, see .logs/health-check/$ts/$t.log)"
    TIER_STATUS[$t]=FAIL
  fi
}

for t in "${tiers[@]}"; do
  if ! should_run "$t"; then
    warn "skipping $t (not in --only)"
    TIER_STATUS[$t]=SKIP
    continue
  fi
  run_one "$t"
done

# ---------- module-level aggregation ----------
# Parse ruff JSON into a module-grouped summary.
ruff_summary="$out_dir/ruff.by-module.tsv"
: > "$ruff_summary"
if [[ -s "$out_dir/ruff.log" ]]; then
  uv run python - "$out_dir/ruff.log" "$ruff_summary" <<'PY' || true
import json, sys, os, collections, pathlib

raw = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
out = pathlib.Path(sys.argv[2])

# ruff prints JSON; bail out gracefully if it didn't (e.g. uv error).
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    out.write_text("# ruff did not emit JSON (see ruff.log)\n", encoding="utf-8")
    sys.exit(0)

def bucket(path: str) -> str:
    # Group api/routes/blast/foo.py -> routes/blast
    #       api/services/foo.py     -> services/foo.py
    p = pathlib.PurePosixPath(path)
    parts = p.parts
    if "api" in parts:
        i = parts.index("api")
        rest = parts[i + 1 :]
    else:
        rest = parts
    if not rest:
        return path
    if len(rest) >= 3 and rest[0] in {"routes", "services", "tasks", "tests"}:
        return f"{rest[0]}/{rest[1]}"
    return f"{rest[0]}/{rest[1]}" if len(rest) >= 2 else rest[0]

by_bucket = collections.Counter()
by_rule_per_bucket = collections.defaultdict(collections.Counter)
for item in data:
    b = bucket(item["filename"])
    by_bucket[b] += 1
    by_rule_per_bucket[b][item["code"]] += 1

lines = ["module\terrors\ttop_rules"]
for b, n in by_bucket.most_common():
    rules = ", ".join(f"{r}:{c}" for r, c in by_rule_per_bucket[b].most_common(5))
    lines.append(f"{b}\t{n}\t{rules}")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

# Parse pytest output for FAILED / ERROR lines and bucket by test file.
pytest_summary="$out_dir/pytest.by-file.tsv"
: > "$pytest_summary"
if [[ -s "$out_dir/pytest.log" ]]; then
  uv run python - "$out_dir/pytest.log" "$pytest_summary" <<'PY' || true
import re, sys, pathlib, collections

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
out = pathlib.Path(sys.argv[2])

# Lines like:
#   FAILED api/tests/test_foo.py::test_bar - AssertionError: ...
#   ERROR  api/tests/test_baz.py - ImportError: ...
pat = re.compile(r"^(FAILED|ERROR)\s+(api/tests/[^:\s]+)", re.MULTILINE)
counts = collections.Counter()
for kind, path in pat.findall(text):
    counts[(path, kind)] += 1

# Also surface collection-time errors (top-level summary "ERRORS")
files = collections.Counter()
for (p, kind), n in counts.items():
    files[p] += n

# Pull the final summary line if present, e.g. "1 failed, 807 passed in 68.08s".
summary_line = ""
for m in re.finditer(r"=+\s*([^=]+?)\s*=+\s*$", text, re.MULTILINE):
    s = m.group(1).strip()
    if "passed" in s or "failed" in s or "error" in s:
        summary_line = s
if not summary_line:
  for line in reversed(text.splitlines()):
    if re.search(r"\b(failed|passed|error|errors)\b", line):
      summary_line = line.strip()
      break

lines = [f"# {summary_line}" if summary_line else "# (no pytest summary line found)"]
lines.append("test_file\tfailures")
for p, n in files.most_common():
    lines.append(f"{p}\t{n}")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

# Parse mypy output for "found N errors in M files" + group by file.
mypy_summary="$out_dir/mypy.by-file.tsv"
: > "$mypy_summary"
if [[ -s "$out_dir/mypy.log" ]]; then
  uv run python - "$out_dir/mypy.log" "$mypy_summary" <<'PY' || true
import re, sys, pathlib, collections

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
out = pathlib.Path(sys.argv[2])

pat = re.compile(r"^(api/[^:]+):\d+:\s*error:", re.MULTILINE)
counts = collections.Counter(pat.findall(text))

m = re.search(r"^Found (\d+) errors? in (\d+) files?", text, re.MULTILINE)
summary = m.group(0) if m else "(mypy ran)"
lines = [f"# {summary}", "file\terrors"]
for f, n in counts.most_common():
    lines.append(f"{f}\t{n}")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
fi

# ---------- write summary.md ----------
summary="$out_dir/summary.txt"
{
  echo "# elb-dashboard health-check - $ts"
  echo
  echo "Working dir: \`$project_root\`"
  echo
  echo "## Tier results"
  echo
  echo "| Tier | Status | Log |"
  echo "|------|--------|-----|"
  for t in "${tiers[@]}"; do
    s="${TIER_STATUS[$t]:-UNKNOWN}"
    badge="$s"
    case "$s" in
      PASS) badge="PASS" ;;
      FAIL) badge="FAIL" ;;
      SKIP) badge="SKIP" ;;
    esac
    echo "| \`$t\` | $badge | [\`$t.log\`]($t.log) |"
  done
  echo
  if [[ -s "$ruff_summary" ]]; then
    echo "## Ruff - errors per module (top buckets)"
    echo
    echo '```'
    head -n 30 "$ruff_summary"
    echo '```'
    echo
  fi
  if [[ -s "$pytest_summary" ]]; then
    echo "## Pytest - failures per file"
    echo
    echo '```'
    head -n 40 "$pytest_summary"
    echo '```'
    echo
  fi
  if [[ -s "$mypy_summary" ]]; then
    echo "## Mypy - errors per file (top 30)"
    echo
    echo '```'
    head -n 32 "$mypy_summary"
    echo '```'
    echo
  fi
  echo "## Tier exit codes"
  echo
  echo '```'
  for t in "${tiers[@]}"; do
    rc=$(cat "$out_dir/$t.exit" 2>/dev/null || echo "-")
    printf "%-12s exit=%s\n" "$t" "$rc"
  done
  echo '```'
} > "$summary"

# ---------- final report ----------
echo
step "summary"
echo "  out: $out_dir"
echo "  summary: $summary"
echo

failed=0
for t in "${tiers[@]}"; do
  s="${TIER_STATUS[$t]:-UNKNOWN}"
  case "$s" in
    PASS) printf "  ${GREEN}OK${RESET} %-12s\n" "$t" ;;
    FAIL) printf "  ${RED}XX${RESET} %-12s  (.logs/health-check/$ts/%s.log)\n" "$t" "$t"; failed=$((failed+1)) ;;
    SKIP) printf "  ${YELLOW}-${RESET} %-12s  (skipped)\n" "$t" ;;
    *)    printf "    %-12s\n" "$t" ;;
  esac
done
echo
if [[ $failed -eq 0 ]]; then
  printf "${GREEN}All tiers passed.${RESET}\n"
else
  printf "${RED}%d tier(s) failed.${RESET}  See: %s\n" "$failed" "$summary"
fi
exit "$failed"
