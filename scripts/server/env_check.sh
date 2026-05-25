#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

failures=0
warnings=0

pass() { printf '[PASS] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*"; warnings=$((warnings + 1)); }
fail() { printf '[FAIL] %s\n' "$*"; failures=$((failures + 1)); }

check_file() {
  local path="$1"
  if [[ -s "$path" ]]; then
    pass "required file exists: $path"
  else
    fail "required file missing or empty: $path"
  fi
}

printf '== Phase 0.5 Environment Check ==\n'
printf 'cwd: %s\n' "$PWD"

if command -v git >/dev/null 2>&1; then
  printf 'git_branch: %s\n' "$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  printf 'git_sha: %s\n' "$(git rev-parse HEAD 2>/dev/null || echo unknown)"
  if [[ -n "$(git status --porcelain 2>/dev/null || true)" ]]; then
    warn "git working tree is dirty"
    git status --short || true
  else
    pass "git working tree is clean"
  fi
else
  fail "git is not available"
fi

if command -v python3 >/dev/null 2>&1; then
  python3 --version
  pass "python3 is available"
else
  fail "python3 is not available"
fi

check_import() {
  local label="$1"
  local code="$2"
  if python3 - "$label" "$code" <<'PY'
import importlib
import sys

label = sys.argv[1]
mods = sys.argv[2].split("|")
errors = []
for mod in mods:
    try:
        importlib.import_module(mod)
        print(f"{label}: imported {mod}")
        raise SystemExit(0)
    except Exception as exc:
        errors.append(f"{mod}: {exc}")
print(f"{label}: import failed: " + "; ".join(errors))
raise SystemExit(1)
PY
  then
    pass "python import available: $label"
  else
    fail "python import unavailable: $label"
  fi
}

check_import "sqlglot" "sqlglot"
check_import "postgres driver" "psycopg2|psycopg"
check_import "numpy" "numpy"
check_import "pandas" "pandas"
check_import "pytest" "pytest"

printf '\n== Repository Files ==\n'
for f in \
  txt/tpch_indexable_columns.txt \
  txt/tpchs_indexable_columns.txt \
  txt/job_indexable_columns.txt \
  txt/tpch_op_3_create_time.txt \
  txt/tpchs_op_3_create_time.txt \
  txt/job_op_3_create_time.txt; do
  check_file "$f"
done

for f in \
  database/workload/tpchs_noisy.txt \
  database/workload/tpchs_random.txt \
  database/workload/job_random.txt; do
  if [[ -e "$f" ]]; then
    check_file "$f"
  else
    fail "expected first-pass workload file not found: $f"
  fi
done

if python3 - <<'PY'
import json
from pathlib import Path

path = Path("adasel/config/adaselect.json")
cfg = json.loads(path.read_text(encoding="utf-8"))
max_width = int(cfg.get("max_width", -1))
print(f"adasel/config/adaselect.json max_width={max_width}")
raise SystemExit(0 if max_width == 2 else 1)
PY
then
  pass "adaselect.json max_width is 2"
else
  fail "adaselect.json max_width is not 2"
fi

printf '\n== PostgreSQL / HypoPG ==\n'
if python3 - <<'PY'
from database.database_connector import DatabaseConnector

benches = ["tpch", "tpchs", "job"]
errors = []
for bench in benches:
    db = None
    try:
        db = DatabaseConnector(bench, virtual=True, run_num=1)
        one = db.fetch_one_value("SELECT 1")
        if int(one) != 1:
            raise RuntimeError(f"SELECT 1 returned {one!r}")
        print(f"{bench}: PostgreSQL connectivity OK")
        ext_count = db.fetch_one_value("SELECT COUNT(*) FROM pg_extension WHERE extname = 'hypopg'")
        if int(ext_count or 0) <= 0:
            raise RuntimeError("HypoPG extension is not installed in this database")
        db.fetch_one_value("SELECT hypopg_reset()")
        print(f"{bench}: HypoPG availability OK")
    except Exception as exc:
        errors.append(f"{bench}: {exc}")
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
if errors:
    for err in errors:
        print(err)
    raise SystemExit(1)
PY
then
  pass "PostgreSQL connectivity and HypoPG checks passed"
else
  fail "PostgreSQL connectivity or HypoPG check failed"
fi

printf '\n== Summary ==\n'
printf 'failures=%d warnings=%d\n' "$failures" "$warnings"
if (( failures > 0 )); then
  printf 'RESULT: FAIL\n'
  exit 1
fi
printf 'RESULT: PASS\n'
