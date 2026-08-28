---
name: testing
description: >-
  Runs local static syntax and logic verification (lint, compile check, matching unit tests) on
  a set of changed files, with one bounded retry on failure before escalating. Generic reusable
  sub-skill — invoked by opsbuddy-fix Phase 5 for its remediation static-validation step, and
  usable standalone after any code change ("run static checks on these files", "verify this
  diff compiles and lints clean"). Reports a plain PASS/FAIL verdict plus full tool output.
---

# testing

**Argument**: a list of changed file paths (or a short description of what changed, if paths
weren't tracked).

---

## Step 1 — Detect Changed File Types

Group `$ARGUMENTS` by type:
- Python: `*.py` under `python/`, `workflow/`, `scripts/`, `databricks/notebooks/`
- dbt/SQL: `*.sql` under `dbt/models/`
- Everything else: flag and skip static checks (no tooling configured for it)

---

## Step 2 — Run Static Checks

**Python files:**
```
# MCP-preferred (opsbuddy-git-ops, if registered — see plugins/insightops-buddy/mcp-server/README.md)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__run_static_checks(repo_dir="<repo_dir>", files=[<files>])

# Bash fallback
black --check <files>
isort --check <files>
flake8 --max-line-length=120 <files>
python -m py_compile <files>
```
Then, if a matching test file exists (`python/tests/test_<module>.py`):
```
# MCP-preferred (only if run separately from run_static_checks above, which already runs this
# same matching-test-file step itself — use run_pytest for an ad-hoc/retry run instead)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__run_pytest(repo_dir="<repo_dir>", test_path="python/tests/test_<module>.py")

# Bash fallback
pytest python/tests/test_<module>.py -m "not integration" -v
```

**dbt/SQL files:**
```bash
cd dbt && dbt compile --select <model_name> --profiles-dir ~/.dbt
sqlfluff lint <file> --dialect snowflake
```
No MCP path for dbt/SQL checks — `opsbuddy-git-ops` only covers Python static validation.

---

## Step 3 — On Failure

1. Show the failing tool's output in full — do not truncate.
2. Propose a specific, targeted fix (not a broad rewrite).
3. Apply it and re-run the same check **once**.
4. If it still fails after the single retry, stop and escalate back to the caller with the
   failure output rather than looping indefinitely.

---

## Step 4 — Report

Return to the caller:
- Files checked, grouped by type
- Tools run and pass/fail per tool
- Any retry that was needed and its outcome
- A final `PASS` / `FAIL` verdict
