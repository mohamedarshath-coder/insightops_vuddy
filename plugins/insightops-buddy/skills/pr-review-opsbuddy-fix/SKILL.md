---
name: pr-review-opsbuddy-fix
description: >-
  Automated PR review (Mode A) that validates a diff against a confirmed Databricks incident
  root cause via a 7-point checklist (scope, targeted fix, category match, no error-suppression
  anti-patterns, static validation passed, no scope creep, re-run safety), returning a plain
  PASS/FAIL verdict. Invoked from opsbuddy-fix Phase 8 with the root-cause-analysis verdict block
  (ERROR_CATEGORY / ROOT_CAUSE_SUMMARY / CODE_FIX_POSSIBLE / AFFECTED_FILES /
  SUGGESTED_FIX_APPROACH) attached as context — do not invoke standalone without that context,
  since the checklist has nothing to validate the diff against otherwise. For general PR review
  without a confirmed root cause, use the general pr-review skill instead.
---

# pr-review-opsbuddy-fix (Mode A)

**Arguments**: a repo + PR number, plus the root-cause verdict block from opsbuddy-fix Phase 2 /
databricks-debug (`ERROR_CATEGORY`, `ROOT_CAUSE_SUMMARY`, `CODE_FIX_POSSIBLE`, `AFFECTED_FILES`,
`SUGGESTED_FIX_APPROACH`).

The repo is whatever opsbuddy-fix Phase 4 resolved (`get-repo-mapping`) — don't assume
`$GITHUB_REPO`. A job's actual backing repo can differ from whatever that env var happens to be
set to (confirmed in practice: `GITHUB_REPO` pointed at one repo, but the job under diagnosis
needed to target a different one entirely).

---

## Step 1 — Get PR Details

```bash
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py review-pr --pr-number <PR-number> --repo <owner/repo resolved in Phase 4>
```
Also fetch the full diff via PyGitHub, against the **resolved** repo, not `$GITHUB_REPO`:
```python
from github import Auth, Github
from python.utils.config import require
g = Github(auth=Auth.Token(require('GITHUB_TOKEN')))
repo = g.get_repo("<owner>/<repo>")  # the repo opsbuddy-fix Phase 4 resolved for this run
pr = repo.get_pull(<PR-number>)
for f in pr.get_files():
    print(f.filename, f.status, f'+{f.additions}/-{f.deletions}')
```

## Step 2 — Check CI Status

Poll whatever check-status the repo actually has (combined commit status / check runs via
PyGithub). **Zero checks configured is not the same as checks passing** — if the PR/repo has no
CI configured at all, there's nothing to gate on; note that plainly in the verdict rather than
treating silence as either a pass or a blocker. If checks *are* configured and any are red,
diagnose and report what needs fixing before continuing.

---

## Step 3 — The 7-Point Mode A Checklist

Validates the diff against the confirmed root cause, not general style conventions:

1. **Scope** — the diff touches only the file(s) listed in `AFFECTED_FILES`; no unrelated files
   changed.
2. **Targeted** — the specific line(s)/logic identified in `ROOT_CAUSE_SUMMARY` are actually
   modified, not merely adjacent or cosmetic lines.
3. **Category match** — the fix directly addresses the classified `ERROR_CATEGORY` (e.g. a
   Schema Mismatch fix aligns/casts the actual missing column, not a blanket try/except).
4. **No error-suppression anti-patterns** — no bare `except:`, no silently dropping or
   nulling-out bad records, no swallowed exceptions, unless explicitly justified with a code
   comment explaining why that's safe here.
5. **Static validation passed** — the `testing` sub-skill ran clean (lint, syntax, relevant unit
   tests green); attach its captured output.
6. **No scope creep** — no unrelated refactors or pure formatting diffs beyond the minimal fix.
7. **Re-run safety** — re-running the Databricks job after this fix must not double-process,
   duplicate rows, or corrupt data (idempotency check).

Verdict format:
```
MODE A REVIEW — <ERROR_CATEGORY>
1. Scope             : PASS/FAIL — <note>
2. Targeted          : PASS/FAIL — <note>
3. Category match    : PASS/FAIL — <note>
4. No suppression    : PASS/FAIL — <note>
5. Static validation : PASS/FAIL — <note>
6. No scope creep     : PASS/FAIL — <note>
7. Re-run safety      : PASS/FAIL — <note>

VERDICT: PASS | FAIL
```

## Step 4 — Report

Return the verdict block verbatim to the caller (opsbuddy-fix Phase 8). On `FAIL`, the caller
loops back to its remediation phase **once** for a bounded retry before escalating (Jira comment
+ Slack alert) rather than opening/merging a bad PR — this skill only reports the verdict, it
never itself retries the fix, comments on Jira, or merges anything.
