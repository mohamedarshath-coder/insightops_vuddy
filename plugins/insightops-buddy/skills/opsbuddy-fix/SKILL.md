---
name: opsbuddy-fix
description: >-
  Autonomous, end-to-end incident response for a failed Databricks production job run: fetch
  telemetry, classify via databricks-debug + the root-cause-analysis (Cat L) agent, file a Jira
  ticket and carry it through a full Kanban lifecycle (To Do -> In Progress -> In Review -> Done),
  gate on whether a code fix is possible, resolve the backing repo, apply and validate the fix,
  open a PR, run an automated Mode A review, post one threaded 5-stage Slack timeline (detected,
  PR opened, merged, verifying, resolved -- tagged with the job ID at every stage), update Jira,
  log the incident, and publish a Confluence postmortem page. Use whenever given a Databricks job run ID or job ID and asked to fix,
  resolve, or triage a failure end-to-end (e.g. "job 91004 failed, fix it", "run opsbuddy-fix on
  run 48213"). For read-only diagnosis with no fix/PR, use databricks-debug instead.
---

# opsbuddy-fix — Autonomous Pipeline Failure Monitoring & Fix (11 Phases + Confluence)

Takes a failed Databricks job run from "it broke" to "here's a reviewed, merged-ready PR, a
Jira ticket carried through its full Kanban lifecycle, a timeline of Slack updates, and a
published postmortem page" — maintaining a live checklist across 11 phases plus a Confluence
documentation phase. A human still makes the merge decision; this skill never merges its own PR.

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
  `run_static_checks`, `run_pytest`, `get_repo_mapping`, `create_pr`, `find_open_pr`,
  `post_slack_alert`, `log_incident`, `read_file`, `write_file`, `get_job_run`,
  `get_latest_failed_run`, `trigger_job_run`, `get_table_lineage`. This is the only tool set in this
  list whose contract is actually verified against this skill's needs (built and tested for it
  specifically) — prefer it over a generically-registered server below whenever both could do the
  same job.
- The **Atlassian connector** (`mcp__claude_ai_Atlassian__*`) — `getVisibleJiraProjects`,
  `searchJiraIssuesUsingJql`, `createJiraIssue`, `addCommentToJiraIssue`, `transitionJiraIssue`,
  `getTransitionsForJiraIssue`, `getJiraProjectIssueTypesMetadata`, and (for Phase 10.5)
  `createConfluencePage`/`updateConfluencePage`/`getPagesInConfluenceSpace`. Every call needs
  `cloudId` — resolve it **once** per run via
  `mcp__claude_ai_Atlassian__getAccessibleAtlassianResources` (or try the site hostname, e.g.
  `yourorg.atlassian.net`, directly as `cloudId` first) and reuse it for every Jira/Confluence
  call below. **Always call `getTransitionsForJiraIssue` immediately before any
  `transitionJiraIssue`** — the transition names on a real Kanban board (e.g. "Start Progress" vs.
  "In Progress", or a custom "Done" variant) vary per project, exactly like the issue-type
  fallback already does for `createJiraIssue`; match by substring against what comes back rather
  than hardcoding a name, and if nothing matches, say so in the ticket comment and move on rather
  than blocking the whole run on a Kanban-column naming mismatch.
  Bash fallback for Confluence: `workflow/confluence_workflow.py upsert-page` (this plugin's own
  script, new this revision — no MCP-vs-Bash gap here since the Atlassian connector's Confluence
  tools and this script hit the same REST API).
- A **generic Slack MCP server** (e.g. `@modelcontextprotocol/server-slack`, if registered) —
  `slack_post_message`. Lower priority than this plugin's own `post_slack_alert` above: this one's
  exact tool name/args aren't verified against your installed server in this session. Only reach
  for this if `SLACK_WEBHOOK_URL` genuinely isn't configured anywhere *and* `post_slack_alert`'s
  own `SLACK_BOT_TOKEN`+`SLACK_CHANNEL_ID` path (see below) isn't either — `post_slack_alert` can
  now thread its own replies, so there's rarely a reason to reach past it for a generic server.

Phase 1 telemetry, Phase 7 PR creation, Gate 8.5's real-verification trigger, and Phase 10
alerting/incident-logging all now have a verified MCP path via this plugin's own
`opsbuddy-git-ops` — `get_job_run`/`get_latest_failed_run`, `create_pr`/`find_open_pr`,
`trigger_job_run`, and `post_slack_alert`/`log_incident` respectively — separate from the
`databricks-job-lineage`/`github`/generic-`slack` MCP servers that may also be registered; those
other servers' tool contracts aren't verified against this skill's needs (and `trigger_job_run`
here doesn't depend on `databricks-job-lineage`'s own trigger being enabled via its
`DATABRICKS_ALLOW_JOB_TRIGGER` flag), so this plugin's own tools are preferred throughout. This
was the last place Bash was still a silent primary path rather than a genuine last resort:
confirmed in practice, Gate 8.5's real re-run on a Desktop-driven run had to fall back to
whatever bash-like sandbox Desktop has for unrelated tasks, since no MCP tool existed for it —
a different, less-tested execution path than this server, potentially without the same local
env/credentials.

**Argument**: a Databricks job run ID (e.g. `48213`). If only a job ID is known:
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__get_latest_failed_run(job_id="<job-id>")

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
  [ ] 4.  Create Jira ticket (skipped if step 3 found one) — Kanban column: To Do
  [ ] 5.  Transition Jira ticket -> In Progress (Kanban: work has started)
  [ ] 6.  📢 Slack alert 1/5 — incident detected (RCA summary)
  [ ] 7.  ⛔ GATE 3.5 (automated): Feasibility — CODE_FIX_POSSIBLE

PHASE 4 — GIT SETUP
  [ ] 8.  Resolve the backing repo (get-repo-mapping), dedup open PRs, clone +
          create isolated hotfix branch

PHASE 5 — REMEDIATION
  [ ] 9.  Apply code fix
  [ ] 10. Static validation (testing sub-skill)

PHASE 6 — COMMIT & PUSH
  [ ] 11. Commit (standard message convention) + push to GitHub

PHASE 7 — PULL REQUEST
  [ ] 12. Open PR linking hotfix branch → target deployment branch
  [ ] 13. Transition Jira ticket -> In Review (Kanban: awaiting review/merge)
  [ ] 14. 📢 Slack alert 2/5 — PR opened, not yet merged

PHASE 8 — REVIEW
  [ ] 15. Automated PR review (pr-review-opsbuddy-fix, Mode A) vs. root cause
  [ ] 16. ⛔ MERGE APPROVAL GATE (human): approve + confirm merge
  [ ] 17. 📢 Slack alert 3/5 — PR merged
  [ ] 18. 📢 Slack alert 4/5 — verification running (real re-run triggered)
  [ ] 19. ⛔ GATE 8.5 (automated/human): Verify fix against a real re-run
          (alerts 3 and 4 above can fire in either order — see Gate 8.5: a job that can verify
          pre-merge via a Repos/git_source branch swap runs 18 before 16/17; a job that can only
          be verified by merging to main runs 16/17 first)

PHASE 9 — TICKET UPDATE
  [ ] 20. Update Jira ticket (PR link, review verdict, execution status)
  [ ] 21. Transition Jira ticket -> Done (Kanban: only once genuinely resolved)

PHASE 10 — ALERTING & ERROR LOGGING
  [ ] 22. 📢 Slack alert 5/5 — resolved (final summary)
  [ ] 23. Write incident row to Databricks error log table

PHASE 10.5 — CONFLUENCE DOCUMENTATION
  [ ] 24. Create/update the incident postmortem Confluence page

PHASE 11 — SUMMARY
  [ ] 25. Clean up local working clone
  [ ] 26. Print final execution summary
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
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__get_job_run(run_id="$ARGUMENTS")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-run-failure --run-id $ARGUMENTS
```
Capture job name, task key, life-cycle/result state, full error message and stack trace
(untruncated), cluster ID, run parameters, run page URL. This is the error only — not the source
code; that's a separate fetch in Phase 2/4 (see below), since only the stack trace and file
names come from telemetry.

**Also pull real data lineage, best-effort:**
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__get_table_lineage(run_id="$ARGUMENTS")
```
Capture `tables_read`, `tables_written`, and `downstream_consumers` — this is the actual data
blast radius (what else in the workspace reads the tables this run touched), distinct from and in
addition to the task-level "Downstream impact" Phase 3's ticket already reports. This needs
`DATABRICKS_SQL_WAREHOUSE_ID` and Unity Catalog lineage tracking enabled — **if it comes back
with an `error` (not configured, UC lineage off, query failed), don't block or retry: note
lineage as "unavailable" in Phase 3's ticket and move on.** This is enrichment, not a
prerequisite — Phase 2's diagnosis and everything after it proceeds identically whether or not
this call actually returns data.

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
  description="<run metadata + full diagnostics -- standard Markdown, see format note below>",
  additional_fields={"priority": {"name": "High"}, "labels": ["opsbuddy-fix"]})

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py create --project <project> --type Task \
  --summary "[opsbuddy-fix] <job_name> run $ARGUMENTS failed — <ERROR_CATEGORY>" \
  --description "<run metadata + full diagnostics -- standard Markdown>" --priority High --label opsbuddy-fix
```
**Description format — standard Markdown only, never Jira wiki markup.** The Atlassian
connector's `description` renders CommonMark Markdown (`##`/`###` headings, triple-backtick code
fences, `**bold**`) and converts it to ADF itself — it does **not** render Jira/Confluence wiki
markup (`h3.` headings, `{code}...{code}` blocks, `{quote}`). Confirmed in practice: a real run
used wiki markup here and it came out as literal, unrendered text (`h3. Root cause` printed as a
plain line, not a heading) — easy mistake since wiki markup is what a human typing directly into
Jira's own editor would use, but this call doesn't go through that editor. Use exactly this shape:

```markdown
### Run details
Job: <job_name> (job_id <job_id>)
Run ID: <run_id>
Failed task: <task_key> (<source_path>)
Downstream impact: <tasks that went UPSTREAM_FAILED, or "none">
Run page: <run_page_url>

### Data lineage
Tables read: <tables_read, or "none">
Tables written: <tables_written, or "none">
Downstream consumers: <downstream_consumers -- name + type per line, or "none found">
(omit this whole section if Phase 1's get_table_lineage call errored or wasn't configured --
say "unavailable" in one line instead of leaving it out silently, so a reader knows it was
checked and just couldn't be retrieved, not that no one thought to check)

### Error
```
<the real error message, verbatim>
```

### Root cause
ERROR_CATEGORY: <category>

<the actual root-cause paragraph(s) from Phase 2>

### Affected files
<one path per line>

### Suggested fix
<the fix approach, in prose>

CODE_FIX_POSSIBLE: <true/false> (<confidence note>)
```

(`create` automatically falls back to whatever issue type the project actually has — Incident >
Bug > Task > Story — if the requested type doesn't exist.) Populate with job/run ID, error
category, root cause summary, stack trace excerpt, affected files, and — if Phase 2 found a
second, unfixable bug — a plain note describing it as a follow-up needing a human decision.
Capture the ticket key — used in every later branch name, commit, comment. A freshly created
ticket sits in its project's initial Kanban column (typically "To Do"/"Open") — that's fine as-is,
no transition needed yet.

**Move it into the Kanban "doing" column now that automated remediation is actually starting:**
```
# MCP-preferred (Atlassian connector) -- fetch the real transitions first, this board's exact
# name for the "in progress" column isn't guaranteed (confirmed pattern elsewhere in this skill:
# same reasoning as the issue-type fallback above)
mcp__claude_ai_Atlassian__getTransitionsForJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>")
mcp__claude_ai_Atlassian__transitionJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
  transitionId="<id of whichever returned transition name matches 'in progress'/'doing'/'start'>")

# Bash fallback (matches by name against the real available transitions, raises with the full
# list if nothing matches -- never hardcodes a transition name blind)
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py transition <TICKET-KEY> "In Progress"
```

**Slack alert 1/5 — incident detected.** This is the one checkpoint that carries prose, not just
fields: put the plain-English root cause (from Phase 2's reconciled verdict) in `message` so the
channel sees *what actually broke*, not just a category label. Also the one checkpoint that starts
the thread every later alert replies into: it's called with no `thread_ts` (there's nothing to
reply to yet), and if `SLACK_BOT_TOKEN`+`SLACK_CHANNEL_ID` are configured the response includes a
`ts` — **keep that value** (same way you already keep `<TICKET-KEY>` and `<pr_url>` for later
phases) and pass it back as `thread_ts` on alerts 2-5 below, so all five land as one thread instead
of five separate top-level messages. On the plain-webhook path `ts` comes back `None` — nothing to
carry forward, later alerts just post standalone, exactly as before threading existed.
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__post_slack_alert(
  jira_ticket_id="<TICKET-KEY>", job_id="<job_id>", databricks_run_id="$ARGUMENTS",
  error_category="<ERROR_CATEGORY>", execution_status="IN_PROGRESS", stage="incident_detected",
  message="<ROOT_CAUSE_SUMMARY from Phase 2, 2-4 plain-English sentences>")

# Bash fallback -- prints "THREAD_TS=..." on stdout when the bot-token path is configured; carry
# that value into --thread-ts on alerts 2-5 the same way
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --job-id <job_id> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --status IN_PROGRESS --stage incident_detected --message "<ROOT_CAUSE_SUMMARY from Phase 2>"
```

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
default repo. Prefer this plugin's own `get_repo_mapping` (bundled in `opsbuddy-git-ops`) — it
tries Databricks' two official git-linkage mechanisms *and* the heuristic source-scan fallback
in one call, and is guaranteed available whenever this plugin itself is installed:
```
# MCP-preferred (this plugin's own opsbuddy-git-ops -- official Databricks linkage first, then
# falls back to scanning source_content for a hardcoded git URL, all in one call)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__get_repo_mapping(
  source_path="<failed task's source_path>", job_id="<job_id>",
  source_content="<the task source already fetched for Phase 2 -- gives the heuristic fallback something to scan>")

# MCP alternative (databricks-job-lineage, if installed -- some deployed versions of that server
# don't expose this tool at all; official mechanisms only, no heuristic fallback)
mcp__plugin_databricks-job-lineage_databricks-lineage__get_repo_mapping(source_path="<failed task's source_path>", job_id="<job_id>")

# Bash fallback (official mechanisms only -- see the manual heuristic steps below if this errors)
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-repo-mapping \
  --source-path "<failed task's source_path>" --job-id <job_id>
```
Always pass `job_id` and, for the preferred path, `source_content`. `repo_url`/`error: null` →
use that `repo_url`/`branch` for every step below. The response's `resolution_method`
(`databricks_repos` / `job_git_source` / `heuristic_source_scan`) tells you which mechanism
actually resolved it — if it's the heuristic one, note that explicitly in the ticket/report: it's
a strong signal, not a guarantee.

**If you're on the Bash fallback (no built-in heuristic) and it also errors — don't stop yet,
try the same heuristic manually first.** `get_repo_mapping` only knows about Databricks' two
*official* git-linkage mechanisms (a Repos checkout under `/Repos/...`, or a job-level Git source
configured in the Jobs UI). Plenty of real jobs use **neither** — the task's own code just runs a
plain `git clone <url>` itself, which is completely invisible to Databricks' APIs (confirmed in
practice, twice: both test jobs used to validate this pipeline had exactly this shape). Before
giving up:

1. You should already have the failing task's source fetched (Phase 2's diagnosis step needs it
   anyway) — if not, fetch it now via `get_source_file`/the Bash equivalent.
2. Scan that source text for a hardcoded git URL: look for a `git clone` invocation, or a bare
   `https://...` / `git@...` string ending in `.git` (commonly assigned to a variable like
   `REPO_URL`). Take the first match.
3. **Strip any embedded credential before using this further** — a URL like
   `https://ghp_xxx@github.com/owner/repo.git` has a token baked into it; use just
   `https://github.com/owner/repo.git` for cloning (the plugin's own git tools authenticate via
   `GITHUB_TOKEN`/`GIT_ASKPASS`, never via a token embedded in the URL), and treat that embedded
   token as a **live, exposed credential** worth flagging in the ticket regardless of whether it's
   related to this incident's actual fix.
4. Note in the ticket/report that the repo was resolved via this heuristic, not via Databricks'
   own tracked git-linkage — it's a strong signal, not a guarantee (a source file that clones more
   than one repo, or clones something incidental rather than its own project, would fool this).

Only if this heuristic also finds nothing → **stop**, report the error plainly rather than
guessing at a repo.

**Dedup — check for an existing open PR for this run before creating anything:**
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__find_open_pr(repo="<owner/repo>", search_text="<run_id or job_id>")

# Bash fallback -- e.g. via PyGithub's get_pulls(state="open") filtered by title/branch
```
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
`SUGGESTED_FIX_APPROACH`.

**On Claude Code**, do this with the Read/Edit tools directly against `<repo_dir>` — no MCP call
needed, this client has real filesystem access.

**On Claude Desktop, this is the one phase with no built-in fallback** — Desktop has no Bash tool
and no file-editing tool of its own; everything it can do comes from an MCP server. Read/write the
file through this plugin's own `opsbuddy-git-ops`:
```
mcp__plugin_insightops-buddy_opsbuddy-git-ops__read_file(repo_dir="<repo_dir>", path="<AFFECTED_FILE>")
mcp__plugin_insightops-buddy_opsbuddy-git-ops__write_file(repo_dir="<repo_dir>", path="<AFFECTED_FILE>", content="<full new file content>")
```
`write_file` replaces the whole file — read it first, edit the content in-memory, then write the
complete result back; there is no line-level patch tool. Confirm the change actually landed with
`git_status` (or a follow-up `read_file`) before moving to Phase 6. (This closed a real gap: an
earlier Desktop run diagnosed the fix correctly and got as far as creating the hotfix branch, then
had no way to actually write the one-line change and had to halt and hand off to a human.)

Then invoke the **testing** sub-skill for static verification (one bounded retry on failure). If it
still fails: stop, post a Jira comment, send the Phase 10 Slack alert with
`EXECUTION_STATUS=REMEDIATION_FAILED`, write the Databricks incident row, jump to Phase 11.

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

**Always pass the repo explicitly** — never rely on a default, since the job's actual repo
(resolved in Phase 4) can differ from any fixed default (confirmed in practice: a run against a
different repo silently tried to open a PR on the wrong one until this was fixed):
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__create_pr(
  repo="<owner/repo resolved in Phase 4>", branch="<TICKET-KEY>/hotfix-<slug>",
  base="<branch resolved in Phase 4>", title="[<TICKET-KEY>] <job_name> <ERROR_CATEGORY> fix",
  body="<summary of root cause, fix, and validation — same content the Bash fallback auto-generates>")

# Bash fallback (also handles the Jira transition/comment below as a side effect -- the MCP
# path above does not touch Jira at all, so do that yourself as a separate step if you used it)
cd <repo_dir> && python ${CLAUDE_PLUGIN_ROOT}/workflow/git_workflow.py create-pr \
  --branch <TICKET-KEY>/hotfix-<slug> --jira-id <TICKET-KEY> \
  --repo <owner/repo resolved in Phase 4> --base <branch resolved in Phase 4>
```
Capture the PR URL and number. **If you used the MCP path**, `create_pr` deliberately doesn't
touch Jira at all (the Bash fallback's `create-pr` does this automatically as a side effect), so
do the following two steps explicitly yourself:
```
# MCP-preferred (Atlassian connector) -- same "fetch real transitions first" pattern as Phase 3
mcp__claude_ai_Atlassian__getTransitionsForJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>")
mcp__claude_ai_Atlassian__transitionJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
  transitionId="<id of whichever returned transition name matches 'review'>")
mcp__claude_ai_Atlassian__addCommentToJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
  commentBody="opsbuddy-fix: PR opened, awaiting review/merge. PR: <pr_url>")

# Bash fallback (only needed if you used the MCP create_pr path above -- git_workflow.py's own
# create-pr already did both of these for you)
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py transition <TICKET-KEY> "In Review"
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py comment-rich <TICKET-KEY> \
  "opsbuddy-fix: PR opened, awaiting review/merge." --link pr=<pr_url>
```

**Slack alert 2/5 — PR opened, not yet merged.** Send this regardless of which PR-creation path
you used — neither path sends Slack on its own. Pass alert 1's `thread_ts` if you have one.
```
# MCP-preferred
mcp__plugin_insightops-buddy_opsbuddy-git-ops__post_slack_alert(
  jira_ticket_id="<TICKET-KEY>", job_id="<job_id>", databricks_run_id="$ARGUMENTS",
  error_category="<ERROR_CATEGORY>", pr_url="<pr_url>", execution_status="IN_REVIEW",
  stage="pr_opened", thread_ts="<incident's thread ts, if any>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --job-id <job_id> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --pr-url <pr_url> --status IN_REVIEW --stage pr_opened --thread-ts "<incident's thread ts, if any>"
```

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

**Slack alert 4/5 — verification running.** Send this immediately before triggering the real
re-run below, whichever mechanism applies — the channel should see "we're about to run this for
real" before the run itself starts, not only the eventual pass/fail:
```
# MCP-preferred
mcp__plugin_insightops-buddy_opsbuddy-git-ops__post_slack_alert(
  jira_ticket_id="<TICKET-KEY>", job_id="<job_id>", databricks_run_id="$ARGUMENTS",
  error_category="<ERROR_CATEGORY>", pr_url="<pr_url>", execution_status="VERIFYING",
  stage="verification_running", thread_ts="<incident's thread ts, if any>",
  message="Triggering a real re-run of <job_name> (job <job_id>) to verify the fix.")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --job-id <job_id> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --pr-url <pr_url> --status VERIFYING --stage verification_running \
  --thread-ts "<incident's thread ts, if any>" \
  --message "Triggering a real re-run of <job_name> (job <job_id>) to verify the fix."
```

- **Databricks Repos checkout** (`source_path` under `/Repos/...`) → the normal path:
  ```bash
  python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py sync-repo --repo-url <repo_url> --branch <hotfix branch>
  ```
  No tool anywhere (this plugin or `databricks-lineage`) can repoint a Repo's checked-out branch,
  so `sync-repo` above is Bash-only regardless of client. Once the checkout is repointed,
  triggering and blocking on the run itself has a real MCP path bundled with this plugin:
  ```
  # MCP-preferred (this plugin's own opsbuddy-git-ops -- blocks until terminal state, same as
  # the CLI it mirrors; gated on OPSBUDDY_VERIFY_ALLOWLIST/force the same way)
  mcp__plugin_insightops-buddy_opsbuddy-git-ops__trigger_job_run(job_id="<job_id>", timeout_seconds=600, force=<true only after explicit human approval>)

  # Bash fallback
  python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py trigger-and-wait --job-id <job_id> --timeout 600 --force
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

### ⛔ MERGE APPROVAL GATE — required before any of the above can verify against `main`

This skill **never merges its own PR**, and there is deliberately no `merge_pr` tool anywhere in
`opsbuddy-git-ops` on either client — this is not a gap to close. For a job matching the "known
limitation" above (task code clones its own default branch, ignoring any pre-merge override —
confirmed in practice to be this exact plugin's own `test_run` job), Gate 8.5 categorically cannot
verify anything pre-merge: merging to `main` is the only way to make the fix visible to a re-run
at all, which makes the merge decision itself the real gate, not a formality before one.

**Optional: surface CI status, informational only.** This skill deliberately has no CI gate — it
never blocks, polls, retries, or attempts to fix anything based on check results (that's a much
larger, separate concern than this skill takes on; see `git-ci-fix` in the `databricks-job-lineage`
plugin if that's what you actually want). If a GitHub MCP server is connected (e.g. the native
`github` connector) and exposes some way to read the PR's check/status state — the exact tool name
varies by server (`get_pull_request_status`, a combined-status tool, `list_check_runs_for_ref`,
etc.) — call it once and add one line to the approval request below: `CI status: <n>/<total>
passing`, `CI status: no checks found`, or `CI status: <n> failing — <check names>`. This is purely
so the human approving isn't blind to it, not a judgment this skill makes on their behalf — don't
editorialize on what a failure or an absence of checks means (a project with no CI configured looks
identical to one whose CI silently broke, and this skill isn't equipped to tell them apart — that
ambiguity is exactly why there's no gate here, only a line of information). If no such tool is
available, or the call errors, omit the CI status line entirely rather than guessing or blocking
the approval request on it.

Before merging, present a plain-language approval request — don't just say "should I merge?":
```
PR #<n> — <repo>
- Branch: <hotfix branch> → <base>
- Fixes: <one line per AFFECTED_FILE, plain-language what changed>
- Mode A review: <verdict>, <n>/7
- Jira: <TICKET-KEY>, incident logged, alert sent
- CI status: <n>/<total> passing (omit this line entirely if no check-status tool is available)

If you approve, I will:
1. Merge PR #<n> into <base>.
2. Trigger a real re-run of <job_name> (job <job_id>) -- this can write real production data.
3. Report pass/fail on that real run before updating Jira/incident-log to reflect resolution.

Do you approve?
```
Only merge on an explicit yes — not a general "go ahead" from earlier in the conversation, since
that approved the *fix*, not necessarily an unattended merge+re-run of production. Merging itself
may also be blocked by a client-side safety classifier independent of this skill (confirmed in
practice, in Claude Code) — if so, stop and hand the merge link to the human rather than trying
another tool to route around it; pick back up at the real re-run once they confirm it's merged.

**Slack alert 3/5 — PR merged.** Send this the moment a merge is *confirmed* (don't send on the
approval alone — a "yes" doesn't guarantee the merge itself succeeded, especially given the
safety-classifier caveat above; verify the PR actually shows merged, e.g. via `find_open_pr`
coming back empty or a direct merged-state check, before sending):
```
# MCP-preferred
mcp__plugin_insightops-buddy_opsbuddy-git-ops__post_slack_alert(
  jira_ticket_id="<TICKET-KEY>", job_id="<job_id>", databricks_run_id="$ARGUMENTS",
  error_category="<ERROR_CATEGORY>", pr_url="<pr_url>", execution_status="MERGED",
  stage="pr_merged", thread_ts="<incident's thread ts, if any>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --job-id <job_id> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --pr-url <pr_url> --status MERGED --stage pr_merged --thread-ts "<incident's thread ts, if any>"
```
Note the two mechanisms genuinely order alerts 3 and 4 differently, and that's correct, not a
bug to reconcile: a job verifiable pre-merge (Databricks Repos checkout / job-level git_source)
sends alert 4 first, verifies against the unmerged branch, and only reaches this merge gate
afterward; a job matching the "must merge to verify" limitation reaches this gate first, so
alert 3 fires before alert 4.

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

**Close the Kanban loop — transition to Done, but only if `EXECUTION_STATUS` genuinely reflects a
verified resolution** (Gate 8.5 reported real success, or it was legitimately skipped as a
one-time run — never transition to Done off a bare Mode A `PASS` alone, since that's a code
review, not proof the fix runs):
```
# MCP-preferred (Atlassian connector) -- same "fetch real transitions first" pattern as Phase 3/7
mcp__claude_ai_Atlassian__getTransitionsForJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>")
mcp__claude_ai_Atlassian__transitionJiraIssue(cloudId="<cloudId>", issueIdOrKey="<TICKET-KEY>",
  transitionId="<id of whichever returned transition name matches 'done'/'closed'/'resolved'>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/jira_workflow.py transition <TICKET-KEY> "Done"
```
If `EXECUTION_STATUS` is anything other than a verified resolution (`MANUAL_ACTION_REQUIRED`,
`REVIEW_FAILED`, `VERIFICATION_FAILED`, `REMEDIATION_FAILED`), **don't** transition to Done —
leave the ticket at whatever Kanban column it's already in (In Progress/In Review) so a human
sees it as still open, and say so plainly in the final report.

## Phase 10 — Alerting & Error Logging

**Slack alert 5/5 — resolved (final summary).** This is the last of the five checkpoints, not the
only one — by this point the channel has already seen alerts 1-4, so `message` here should be the
one-line verification result (what Gate 8.5 actually found), not a repeat of the RCA from alert 1:
```
# MCP-preferred (this plugin's own opsbuddy-git-ops -- posts via SLACK_BOT_TOKEN+SLACK_CHANNEL_ID
# if configured (threaded reply, same thread as alerts 1-4), else the plain incoming webhook)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__post_slack_alert(
  jira_ticket_id="<TICKET-KEY>", job_id="<job_id>", databricks_run_id="$ARGUMENTS",
  error_category="<ERROR_CATEGORY>", pr_url="<pr_url>", pr_review_verdict="<mode-a-verdict>",
  execution_status="<EXECUTION_STATUS>", stage="resolved", thread_ts="<incident's thread ts, if any>",
  message="<one-line Gate 8.5 verification result, or why it was skipped>")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/slack_workflow.py send-incident-summary \
  --jira-id <TICKET-KEY> --job-id <job_id> --run-id $ARGUMENTS --category "<ERROR_CATEGORY>" \
  --pr-url <pr_url> --verdict <mode-a-verdict> --status <EXECUTION_STATUS> \
  --stage resolved --thread-ts "<incident's thread ts, if any>" \
  --message "<one-line Gate 8.5 verification result, or why it was skipped>"
```
If the run halted before reaching a terminal state (Gate 3.5/Phase 5/Phase 8/Gate 8.5), this is
still the right call to make — just with `EXECUTION_STATUS` set to whichever halt status applies
(`MANUAL_ACTION_REQUIRED`/`REMEDIATION_FAILED`/`REVIEW_FAILED`/`VERIFICATION_FAILED`) so the
channel's timeline ends with an honest outcome rather than silently stopping at alert 3 or 4.
The Databricks incident-log write now has a real MCP path too — this closed Desktop's last
Phase-10 gap (confirmed in practice: a Desktop-driven run correctly reported this step as
unavailable, since only a Bash fallback existed before):
```
# MCP-preferred (this plugin's own opsbuddy-git-ops)
mcp__plugin_insightops-buddy_opsbuddy-git-ops__log_incident(record={...})   # exact shape below

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py log-incident --json-file <path-to-record.json>
```
**The record's keys must match the real table's actual columns exactly** — this table
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

## Phase 10.5 — Confluence Documentation

Publish one incident postmortem page per run — this is the artifact a human re-reads later or
pastes into a broader report, distinct from the Jira ticket (workflow tracking) and the Slack
alerts (point-in-time notifications). Idempotent by title, so re-running this phase (e.g. after
Gate 8.5 finally resolves following a halt) safely updates the same page rather than creating a
duplicate:
```
# MCP-preferred (Atlassian connector)
mcp__claude_ai_Atlassian__getPagesInConfluenceSpace(cloudId="<cloudId>", spaceId="<space>",
  title="<TICKET-KEY>: <job_name> incident")
# → if found, mcp__claude_ai_Atlassian__updateConfluencePage(...) with the existing pageId;
#   otherwise mcp__claude_ai_Atlassian__createConfluencePage(cloudId="<cloudId>", spaceId="<space>",
#   title="<TICKET-KEY>: <job_name> incident", body="<storage-format HTML, see Bash fallback's
#   build_incident_page_html for the exact structure to mirror: metadata table, root cause,
#   data lineage, timeline, how-to-verify, related resources>")

# Bash fallback (this plugin's own new script — builds the same structure, upsert-by-title)
python ${CLAUDE_PLUGIN_ROOT}/workflow/confluence_workflow.py upsert-page \
  --space <CONFLUENCE_SPACE_KEY, default OOP> --title "<TICKET-KEY>: <job_name> incident" \
  --jira-id <TICKET-KEY> --job-name "<job_name>" --run-id $ARGUMENTS --job-id <job_id> \
  --category "<ERROR_CATEGORY>" --rca "<ROOT_CAUSE_SUMMARY>" --repo "<owner/repo>" \
  --branch "<hotfix branch>" --pr-url <pr_url> --verdict "<mode-a-verdict>" \
  --verification "<Gate 8.5 result>" --status <EXECUTION_STATUS> \
  --tables-read "<Phase 1's tables_read, comma-separated, or 'unavailable'>" \
  --tables-written "<Phase 1's tables_written, comma-separated, or 'unavailable'>" \
  --downstream-consumers "<Phase 1's downstream_consumers, comma-separated 'name (type)', or 'unavailable'>"
```
Reuse Phase 1's `get_table_lineage` result here too — don't re-fetch it. Pass `"unavailable"`
(not an empty string) for any of the three lineage fields if that call errored or was never
configured, so the page distinguishes "checked, found nothing" from "never checked" the same way
Phase 3's ticket does.
Capture the page URL for Phase 11's summary. If neither the Atlassian connector's Confluence
tools nor `CONFLUENCE_BASE_URL`/`CONFLUENCE_EMAIL`/`CONFLUENCE_API_TOKEN` are configured, note
that plainly in the final report — same treatment as a missing `SLACK_WEBHOOK_URL` in Phase 10,
not a silent skip and not a hard failure of the whole run.

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
  Jira         : <TICKET-KEY> -- <final Kanban column, e.g. Done, or In Progress/In Review if
                 halted before a verified resolution>
  Slack alerts : <n>/5 sent (1 detected, 2 pr_opened, 3 pr_merged, 4 verifying, 5 resolved --
                 list any of the 5 that never fired and why, e.g. "3/4 skipped: halted at Gate 3.5")
  Databricks row: <incident_id/skipped>
  Confluence   : <page_url/skipped (reason)>
══════════════════════════════════════
```
If the run halted at Gate 3.5, Phase 5, Phase 8, or Gate 8.5, state clearly which phase it
stopped at and what manual action is now required — and that Slack alert 5/5 and Phase 10.5's
Confluence page still fire even on a halt (reporting the halt itself), while the Jira ticket
correctly stays off "Done" per Phase 9's rule above.
