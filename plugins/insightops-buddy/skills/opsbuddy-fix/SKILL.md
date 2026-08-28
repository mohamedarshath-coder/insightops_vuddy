---
name: opsbuddy-fix
description: >-
  Autonomous, end-to-end incident response for a failed Databricks production job run, across 11
  phases: fetch job telemetry, classify the error into one of 11 standardized categories via the
  databricks-debug sub-skill and the root-cause-analysis (Cat L) agent, create a Jira ticket,
  gate on whether a code fix is genuinely possible, resolve the actual backing GitHub repo, apply
  and statically validate the fix (testing sub-skill), commit and push to GitHub, open a pull
  request, run an automated PR review (pr-review-opsbuddy-fix, Mode A) against the confirmed
  root cause, update the Jira ticket, send a Slack incident alert, log the incident to
  Databricks, and print a final execution summary. Use whenever the user gives a Databricks job
  run ID or job ID and asks to fix, resolve, or triage a failure end-to-end (e.g. "job 91004
  failed, fix it", "run opsbuddy-fix on run 48213", "handle this Databricks incident"). For
  read-only diagnosis with no fix/PR, use databricks-debug directly instead.
---

# opsbuddy-fix — Autonomous Pipeline Failure Monitoring & Fix (11 Phases)

Takes a failed Databricks job run from "it broke" to "here's a reviewed, merged-ready PR and a
logged incident" — maintaining a live checklist across 11 phases. A human still makes the merge
decision; this skill never merges its own PR.

**This plugin bundles its own copies of `workflow/*.py`, `python/utils/*.py`, and its own MCP
server** (under `${CLAUDE_PLUGIN_ROOT}`) — installed via this marketplace, its `opsbuddy-git-ops`
MCP server (declared in `.mcp.json`) starts automatically; every step below still has a Bash
fallback for a plain Claude Code checkout with no plugin installed.

**MCP tool naming**: a plugin's own bundled MCP server is namespaced as
`mcp__plugin_<plugin-name>_<server-name>__<tool-name>` — so this plugin's tools are
`mcp__plugin_insightops-buddy_opsbuddy-git-ops__*`, **not** the bare `mcp__opsbuddy-git-ops__*`
you'd get from manually registering the same server directly in `claude_desktop_config.json`. If
you previously added `opsbuddy-git-ops` by hand there, remove that entry once this plugin is
installed via the marketplace, to avoid registering the same server twice under two different
names.

Prefer the MCP call whenever the matching server is registered and reachable; fall back to the
Bash command (prefixed `${CLAUDE_PLUGIN_ROOT}`) whenever a server isn't registered, or a call
fails/times out — never block the run on an MCP server being present:
- `databricks-lineage`, from the separate `databricks-job-lineage` plugin (if installed) —
  `get_job_run`, `get_latest_failed_run`, `get_repo_mapping`, `trigger_job_run` (only present when
  that server's `DATABRICKS_ALLOW_JOB_TRIGGER=true`). **Its plugin/server names below
  (`mcp__plugin_databricks-job-lineage_databricks-lineage__*`) are inferred from that plugin's
  directory layout, not verified against its actual `plugin.json`/`.mcp.json` (it's a separate,
  private repo)** — confirm the real prefix with `/mcp` once both plugins are installed, and
  adjust if it differs.
- `opsbuddy-git-ops`, from **this** plugin's own bundled `mcp-server/` (see this repo's top-level
  README) — `git_clone`, `git_create_branch`, `git_status`, `git_commit`, `git_push`,
  `run_static_checks`, `run_pytest`.
- The **Atlassian connector** (`mcp__claude_ai_Atlassian__*`) — `getVisibleJiraProjects`,
  `searchJiraIssuesUsingJql`, `createJiraIssue`, `addCommentToJiraIssue`, `transitionJiraIssue`,
  `getJiraProjectIssueTypesMetadata`. Every call needs `cloudId` — resolve it **once** per run via
  `mcp__claude_ai_Atlassian__getAccessibleAtlassianResources` (or try the site hostname, e.g.
  `yourorg.atlassian.net`, directly as `cloudId` first) and reuse it for every Jira call below.
- A **Slack MCP server** (e.g. `@modelcontextprotocol/server-slack`, if registered) — `slack_post_message`.
  Unlike the tools above, this one's exact tool name/args aren't verified against your installed
  server in this session — confirm against its actual tool list before relying on it, and note it
  needs a channel ID (`SLACK_CHANNEL_ID` or similar), which `send-incident-summary` doesn't need
  today since it posts via `SLACK_WEBHOOK_URL` instead.

PR creation (Phase 7) and the Phase 10 Databricks incident-log write still have **no MCP path** —
Phase 7 stays Bash-only even though a `github` MCP server may be registered separately (its tool
contract isn't verified against this skill's needs), and there is no MCP write tool anywhere for
the incident-log table.

**Argument**: a Databricks job run ID (e.g. `48213`). If only a job ID is known:
```
# MCP-preferred (databricks-lineage)
mcp__plugin_databricks-job-lineage_databricks-lineage__get_latest_failed_run(job_id="<job-id>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-latest-failed-run --job-id <job-id>
```

Uses **GitHub** (not Azure DevOps) and **Slack** (not an Email MCP) — this bundle's actual
infrastructure. Jira project key is not fixed to `OPS` — pass whichever real project you use
(confirmed in practice: this org's real Jira has no `OPS` project, only `SCRUM`).

---

## Live Checklist

Display this at the start; reprint with each completed step marked `[x]`.

```
OPSBUDDY-FIX — Run $ARGUMENTS
══════════════════════════════════════
PHASE 0 — PREFLIGHT
  [ ] 0.  Verify GitHub + Jira access before starting

PHASE 1 — TELEMETRY
  [ ] 1.  Get job run details

PHASE 2 — DIAGNOSE
  [ ] 2.  Classify error & root cause (databricks-debug sub-skill +
          root-cause-analysis (Cat L) agent, adversarial double-check)

PHASE 3 — TICKET
  [ ] 3.  Check for an existing open incident ticket for this run (dedup)
  [ ] 4.  Create Jira ticket (skipped if step 3 found one)
  [ ] 5.  ⛔ GATE 3.5 (automated): Feasibility — CODE_FIX_POSSIBLE

PHASE 4 — GIT SETUP
  [ ] 6.  Resolve the backing repo (get-repo-mapping), dedup open PRs, clone +
          create isolated hotfix branch

PHASE 5 — REMEDIATION
  [ ] 7.  Apply code fix
  [ ] 8.  Static validation (testing sub-skill)

PHASE 6 — COMMIT & PUSH
  [ ] 9.  Commit (standard message convention) + push to GitHub

PHASE 7 — PULL REQUEST
  [ ] 10. Open PR linking hotfix branch → target deployment branch

PHASE 8 — REVIEW
  [ ] 11. Automated PR review (pr-review-opsbuddy-fix, Mode A) vs. root cause
  [ ] 12. ⛔ GATE 8.5 (automated/human): Verify fix against a real re-run

PHASE 9 — TICKET UPDATE
  [ ] 13. Update Jira ticket (PR link, review verdict, execution status)

PHASE 10 — ALERTING & ERROR LOGGING
  [ ] 14. Send Slack incident alert
  [ ] 15. Write incident row to Databricks error log table

PHASE 11 — SUMMARY
  [ ] 16. Clean up local working clone
  [ ] 17. Print final execution summary
```

---

## Phase 0 — Preflight

```
# MCP-preferred (Atlassian connector) -- resolve cloudId first, reuse it all run
mcp__claude_ai_Atlassian__getAccessibleAtlassianResources()
mcp__claude_ai_Atlassian__getVisibleJiraProjects(cloudId="<cloudId>", searchString="<project>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py check-access
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py check-access --project <your Jira project key>
```
`git_workflow.py check-access` has no MCP equivalent — always run it (or otherwise confirm
`GITHUB_TOKEN`/`GITHUB_REPO` are valid) regardless of which Jira path is used. Stop here on any
failure — do not discover a permission gap mid-run. If the given Jira project doesn't exist (empty
result from either path above), ask the user for the real one rather than assuming `OPS`.

## Phase 1 — Telemetry

```
# MCP-preferred (databricks-lineage)
mcp__plugin_databricks-job-lineage_databricks-lineage__get_job_run(run_id="$ARGUMENTS")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-run-failure --run-id $ARGUMENTS
```
Capture job name, task key, life-cycle/result state, full error message and stack trace
(untruncated), cluster ID, run parameters, run page URL. This is the error only — not the source
code; that's a separate fetch in Phase 2/4 (see below), since only the stack trace and file
names come from telemetry.

## Phase 2 — Diagnose

Invoke the **databricks-debug** sub-skill with the Phase 1 telemetry. It maps the stack trace
into one of 11 standardized error categories (Schema Mismatch, OOM/Executor Lost, Null
Pointer/NoneType, Syntax Error, Permission/Access Denied, Data Not Found at Source, Cluster
Timeout/Startup Failure, Dependency/Library Import Error, Data Skew/Partition Explosion,
Upstream Task Dependency Failure, Infrastructure/Cloud Provider Error) and spawns **two
independent** `root-cause-analysis` (Cat L) agent instances — each given the real source content
fetched via GitHub (see Phase 4's repo resolution; do this lookup early enough to hand real
source to both agents, not just the error message) — reconciling them into one verdict:
```
ERROR_CATEGORY: <one of the 11 standardized categories>
ROOT_CAUSE_SUMMARY: <2-4 sentences>
CODE_FIX_POSSIBLE: <true|false>
AFFECTED_FILES: <comma-separated repo-relative paths, or "none">
SUGGESTED_FIX_APPROACH: <concrete, minimal, one-paragraph plan>
CONFIDENCE: <high|medium|low>
```
Carry this verdict forward — it drives Gate 3.5, Phase 5, and the Mode A review in Phase 8.

**A single failed run can surface more than one independent bug.** If the two agents' reasoning
uncovers multiple distinct defects (confirmed in practice: one run had two separate broken
models), don't force one `CODE_FIX_POSSIBLE` answer to cover all of them — report a verdict per
bug, fix only the ones both agents agree are safely fixable, and flag the rest as
`MANUAL_ACTION_REQUIRED` in the ticket rather than guessing at a fix neither agent could
confidently propose (e.g. a join with no valid key anywhere in the schema is a data-model gap,
not a rename).

## Phase 3 — Ticket

**Dedup first:**
```
# MCP-preferred (Atlassian connector)
mcp__claude_ai_Atlassian__searchJiraIssuesUsingJql(cloudId="<cloudId>",
  jql='project = <project> AND labels = opsbuddy-fix AND text ~ "$ARGUMENTS"')

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py find-incident --project <project> --run-id $ARGUMENTS
```
If found, reuse that ticket and skip to whichever phase its state implies. Otherwise create one:
```
# MCP-preferred (Atlassian connector) -- check available issue types first, since a project may
# not have "Task"; fall back through Incident > Bug > Task > Story, same order the Bash path uses
mcp__claude_ai_Atlassian__getJiraProjectIssueTypesMetadata(cloudId="<cloudId>", projectIdOrKey="<project>")
mcp__claude_ai_Atlassian__createJiraIssue(cloudId="<cloudId>", projectKey="<project>",
  issueTypeName="<first available of Incident/Bug/Task/Story>",
  summary="[opsbuddy-fix] <job_name> run $ARGUMENTS failed — <ERROR_CATEGORY>",
  description="<run metadata + full diagnostics markdown>",
  additional_fields={"priority": {"name": "High"}, "labels": ["opsbuddy-fix"]})

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py create --project <project> --type Task \
  --summary "[opsbuddy-fix] <job_name> run $ARGUMENTS failed — <ERROR_CATEGORY>" \
  --description "<run metadata + full diagnostics markdown>" --priority High --label opsbuddy-fix
```
(`create` automatically falls back to whatever issue type the project actually has — Incident >
Bug > Task > Story — if the requested type doesn't exist.) Populate with job/run ID, error
category, root cause summary, stack trace excerpt, affected files, and — if Phase 2 found a
second, unfixable bug — a plain note describing it as a follow-up needing a human decision.
Capture the ticket key — used in every later branch name, commit, comment.

### ⛔ GATE 3.5 — Feasibility (automated)

- `CODE_FIX_POSSIBLE == true` (for at least one bug this run) → proceed to Phase 4 for those.
- `CODE_FIX_POSSIBLE == false` for everything found → **halt**: post a Jira comment explaining
  why this needs manual action —
  ```
  # MCP-preferred
  mcp__claude_ai_Atlassian__addCommentToJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
    commentBody="<explanation of why this needs manual action>")

  # Bash fallback
  python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py comment <TICKET-KEY> "<explanation>"
  ```
  — send the Phase 10 Slack alert with `EXECUTION_STATUS=MANUAL_ACTION_REQUIRED`, write the
  Databricks incident row, jump to Phase 11.

## Phase 4 — Git Setup

Resolve the real backing repo from the failed task's `source_path` — never assume a fixed
default repo:
```
# MCP-preferred (databricks-lineage)
mcp__plugin_databricks-job-lineage_databricks-lineage__get_repo_mapping(source_path="<failed task's source_path>", job_id="<job_id>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-repo-mapping \
  --source-path "<failed task's source_path>" --job-id <job_id>
```
Always pass `job_id`. `repo_url`/`error: null` → use that `repo_url`/`branch` for every step
below. `error` set → **stop**, report the error plainly rather than guessing at a repo.

**Dedup — check for an existing open PR for this run before creating anything**, e.g. via
PyGithub's `get_pulls(state="open")` filtered by title/branch referencing this run/job ID.
Found one → reuse it, skip straight to Phase 8. None found → proceed:
```
# MCP-preferred (opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__git_clone(repo_url="<repo_url from above>", target_dir="<TICKET-KEY>")
mcp__plugin_insightops-buddy_opsbuddy-git-ops__git_create_branch(repo_dir="<repo_dir returned above>",
  branch="<TICKET-KEY>/hotfix-<slug-from-error-category>", base="<branch from above>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py clone --repo-url <repo_url from above> \
  --target-dir tmp/opsbuddy-fix/<TICKET-KEY>
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py create-branch --repo-dir tmp/opsbuddy-fix/<TICKET-KEY> \
  --branch <TICKET-KEY>/hotfix-<slug-from-error-category> --base <branch from above>
```
Capture whichever path came back as `<repo_dir>` (the MCP tool's own `repo_dir`, or
`tmp/opsbuddy-fix/<TICKET-KEY>` in Bash mode) — every phase below refers to it as `<repo_dir>`.

## Phase 5 — Remediation & Static Validation

Read every file in `AFFECTED_FILES` fully (never patch blind), apply the minimal fix per
`SUGGESTED_FIX_APPROACH`. Then invoke the **testing** sub-skill for static verification (one
bounded retry on failure). If it still fails: stop, post a Jira comment, send the Phase 10 Slack
alert with `EXECUTION_STATUS=REMEDIATION_FAILED`, write the Databricks incident row, jump to
Phase 11.

## Phase 6 — Commit & Push

```
# MCP-preferred (opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__git_commit(repo_dir="<repo_dir>",
  message="<TICKET-KEY>: fix <ERROR_CATEGORY> in <job_name>", files=["<path1>", "<path2>"])
mcp__plugin_insightops-buddy_opsbuddy-git-ops__git_push(repo_dir="<repo_dir>", branch="<TICKET-KEY>/hotfix-<slug>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py commit --repo-dir <repo_dir> \
  --message "<TICKET-KEY>: fix <ERROR_CATEGORY> in <job_name>" --files <path1>,<path2>,...
python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py push --repo-dir <repo_dir> \
  --branch <TICKET-KEY>/hotfix-<slug>
```

## Phase 7 — Pull Request

Bash-only for now — no MCP-preferred path is wired here yet, even if a `github` MCP server is
registered separately (its tool contract isn't verified against this skill; see
this repo's top-level README.md).

**Always pass `--repo`** — `create-pr` defaults to `$GITHUB_REPO`, which will be wrong whenever
the job's actual repo (resolved in Phase 4) differs from that default (confirmed in practice: a
run against a different repo silently tried to open a PR on the wrong one until this was fixed):
```bash
cd <repo_dir> && python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py create-pr \
  --branch <TICKET-KEY>/hotfix-<slug> --jira-id <TICKET-KEY> \
  --repo <owner/repo resolved in Phase 4> --base <branch resolved in Phase 4>
```
Capture the PR URL and number.

## Phase 8 — Automated PR Review

Spawn the **pr-review-opsbuddy-fix** skill (Mode A), passing the repo, PR number, and the Phase 2
root-cause verdict. Returns `PASS`/`FAIL` via its 7-point checklist.

- `PASS` → Gate 8.5.
- `FAIL` → loop back to Phase 5 **once** (bounded retry). Fails again → stop, Jira comment, Phase
  10 Slack alert with `EXECUTION_STATUS=REVIEW_FAILED`, Databricks incident row, jump to Phase 11.

### ⛔ GATE 8.5 — Verify Fix Against a Real Re-Run

A Mode A `PASS` is a code review, not proof the job runs now. Re-running a real job can write
real production data from unmerged code, so this is gated on `OPSBUDDY_VERIFY_ALLOWLIST` /
explicit human approval either way. **The actual re-run mechanism depends on how the job links
to git — check which one before assuming `sync-repo` will work:**

- **Databricks Repos checkout** (`source_path` under `/Repos/...`) → the normal path:
  ```bash
  python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py sync-repo --repo-url <repo_url> --branch <hotfix branch>
  python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py trigger-and-wait --job-id <job_id> --timeout 600
  ```
  `databricks-lineage` has no write tool for repointing a Repo's checked-out branch, so
  `sync-repo` above is Bash-only regardless of client. Once the checkout is repointed, triggering
  and polling the run itself can use MCP instead:
  ```
  # MCP-preferred (databricks-lineage; only present when its DATABRICKS_ALLOW_JOB_TRIGGER=true)
  mcp__plugin_databricks-job-lineage_databricks-lineage__trigger_job_run(job_id="<job_id>")
  mcp__plugin_databricks-job-lineage_databricks-lineage__get_job_run(run_id="<new run_id>")   # poll until result_state is set
  ```
- **Job-level Git source** (the job's own `settings.git_source` points at a repo — no `/Repos/...`
  checkout exists at all; `sync-repo` will fail with "path doesn't exist" here — confirmed in
  practice). There is no bundled CLI command for this yet — temporarily update the job's own
  `git_source.git_branch` directly via the SDK, run it, then restore it to the original branch
  regardless of outcome:
  ```python
  from workflow.databricks_workflow import DatabricksClient
  from databricks.sdk.service import jobs as dbx_jobs
  client = DatabricksClient().client
  job = client.jobs.get(job_id=<job_id>)
  gs = job.settings.git_source
  client.jobs.update(job_id=<job_id>, new_settings=dbx_jobs.JobSettings(
      git_source=dbx_jobs.GitSource(git_url=gs.git_url, git_provider=gs.git_provider, git_branch="<hotfix branch>")))
  # ... trigger-and-wait as above ...
  # then restore: git_branch=gs.git_branch (the original)
  ```
  **Known limitation**: some jobs' own task code does its own internal git clone independent of
  the job's `git_source` (confirmed in practice — a wrapper notebook cloned its dependencies
  itself, hardcoded to the default branch, silently ignoring this override entirely). If the
  re-run's result doesn't reflect the fix at all, treat that as **inconclusive**, not
  `VERIFICATION_FAILED` — report `Verified: skipped (re-run mechanism can't validate a pre-merge
  branch for this job)` and say why, rather than implying the fix itself is wrong.

Real success → Phase 9. Genuine failure (dbt/job actually re-ran the fix and it still broke) →
loop back to Phase 5 once (same bounded budget as Phase 8's retry). One-time `jobs.submit()` run
with no `job_id` → skip this gate and note why.

## Phase 9 — Ticket Update

```
# MCP-preferred (Atlassian connector)
mcp__claude_ai_Atlassian__addCommentToJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
  commentBody="opsbuddy-fix: PR opened and passed Mode A review (<verdict>). Status: <EXECUTION_STATUS>. PR: <pr_url>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py comment-rich <TICKET-KEY> \
  "opsbuddy-fix: PR opened and passed Mode A review (<verdict>). Status: <EXECUTION_STATUS>." \
  --link pr=<pr_url>
```

## Phase 10 — Alerting & Error Logging

```
# MCP-preferred (a Slack MCP server, if registered -- tool name/args unverified in this session,
# confirm against your installed server's actual tool list; needs a channel ID, unlike the
# Bash path below which posts via a pre-configured incoming webhook)
mcp__slack__slack_post_message(channel_id="<incident-channel-id>",
  text="opsbuddy-fix: <TICKET-KEY> <ERROR_CATEGORY> — <EXECUTION_STATUS>. PR: <pr_url>. Review: <mode-a-verdict>.")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --pr-url <pr_url> --verdict <mode-a-verdict> --status <EXECUTION_STATUS>
```
No MCP write tool exists anywhere for the Databricks incident-log table — this step is Bash-only
regardless of client:
```bash
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py log-incident --json-file <path-to-record.json>
```
**The JSON record's keys must match the real table's actual columns exactly** — this table
predates this plugin (built for an earlier email-alert version of this design, before the pivot
to Slack), so its column names don't match this skill's own vocabulary one-for-one. Verified
against the real table (`dev.ops_incidents.incident_log`, or whatever `DATABRICKS_OPS_INCIDENT_TABLE`
points at) via `DESCRIBE TABLE`; write exactly this shape:
```json
{
  "incident_id": "<TICKET-KEY>",
  "jira_ticket_id": "<TICKET-KEY>",
  "databricks_job_id": <job_id, as a number, not a string>,
  "databricks_run_id": <run_id, as a number, not a string>,
  "job_name": "<job_name>",
  "task_key": "<failing task_key>",
  "error_category": "<ERROR_CATEGORY>",
  "root_cause_summary": "<ROOT_CAUSE_SUMMARY>",
  "stack_trace_excerpt": "<short excerpt, not the full trace>",
  "code_fix_possible": <true|false>,
  "target_repo": "<owner/repo resolved in Phase 4>",
  "branch_name": "<hotfix branch>",
  "commit_sha": "<sha from Phase 6, or empty string if not reached>",
  "pr_url": "<pr_url, or empty string if not reached>",
  "pr_review_verdict": "<Mode A verdict, or empty string if not reached>",
  "execution_status": "<EXECUTION_STATUS>",
  "severity": "High",
  "detected_at": "<ISO timestamp of Phase 1's telemetry fetch -- this column has no default, the insert fails outright without it>",
  "resolved_at": "<ISO timestamp of this Phase 10 write, or omit if not yet resolved>",
  "email_sent": <true|false -- this table has no Slack-specific column; reuse this one to mean "an alert was sent" regardless of channel, until/unless the table is renamed>,
  "email_recipients": "<the Slack channel or webhook target actually used, or empty string>"
}
```
Confirmed working end-to-end with this exact shape (insert, then read back, then delete a
test row) — an earlier attempt using this skill's own natural field names (`job_id`, `repo`,
`slack_sent`, etc.) failed with `UNRESOLVED_COLUMN`/`DELTA_INSERT_COLUMN_MISMATCH` against the
real table. If a future table redesign renames `email_sent`/`email_recipients` to something
Slack-native, update this block to match — don't silently drift back to guessed field names.

If `SLACK_WEBHOOK_URL`/a Slack MCP server, or the Databricks incident-log table isn't configured,
note that plainly in the final report rather than treating it as a silent no-op or a hard failure.

## Phase 11 — Summary

Clean up the isolated clone: `rm -rf <repo_dir>` in Bash mode. `opsbuddy-git-ops` exposes no
delete tool by design (minimal write surface — see its README's safety model), so clones made via
MCP accumulate under its workdir; note that plainly in the final report rather than silently
leaving it unaddressed, and clean it up manually/periodically outside this skill. Then print:
```
<✅ | ⚠️> <TICKET-KEY> — <EXECUTION_STATUS>
══════════════════════════════════════
  Job/Run      : <job_name> / $ARGUMENTS
  Category     : <ERROR_CATEGORY>
  Repo/Branch  : <repo> / <branch>
  PR           : <pr_url>
  Review       : <PASS/FAIL>
  Verified     : <PASS/FAIL/skipped (one-time run)/skipped (not approved)/skipped (mechanism
                 can't validate this job)>
  Jira         : <status>
  Slack sent   : <yes/no>
  Databricks row: <incident_id/skipped>
══════════════════════════════════════
```
If the run halted at Gate 3.5, Phase 5, Phase 8, or Gate 8.5, state clearly which phase it
stopped at and what manual action is now required.
