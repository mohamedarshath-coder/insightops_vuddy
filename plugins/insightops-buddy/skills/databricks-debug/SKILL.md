---
name: databricks-debug
description: >-
  Diagnoses a failed Databricks job run: fetches telemetry, classifies the failure into one of
  11 standardized error categories, and spawns two independent root-cause-analysis (Cat L) agent
  instances to adversarially confirm whether a code fix is genuinely possible before anything
  gets changed. Use standalone for ad-hoc triage of a Databricks job failure ("job 91004 failed,
  what's wrong with it", "diagnose run 48213"), or invoked as Phase 2 of opsbuddy-fix with
  telemetry already fetched. Read-only — never applies a fix or opens a PR itself.
---

# databricks-debug

**Argument**: a Databricks job run ID. Used standalone for ad-hoc triage, or invoked from
opsbuddy-fix Phase 2 with telemetry already fetched (skip Step 1 in that case).

---

## Step 1 — Gather Telemetry, Source, and Repo

```
# MCP-preferred (databricks-lineage, if registered)
mcp__plugin_databricks-job-lineage_databricks-lineage__get_job_run(run_id="$ARGUMENTS")

# Bash fallback
python ${CLAUDE_PLUGIN_ROOT}/workflow/databricks_workflow.py get-run-failure --run-id $ARGUMENTS
```
Capture the full stack trace, error message, cluster ID, and task parameters — do not truncate.

**Also fetch the failing task's actual source now** (`get_source_file` / the Bash equivalent on
its `source_path`) — do this here, not later, since Step 3's agents have no MCP/Databricks access
of their own (`tools: Read, Grep, Glob, Bash` only) and can't fetch it themselves.

**Then resolve the repo it lives in.** Prefer this plugin's own `get_repo_mapping` (bundled in
`opsbuddy-git-ops`), passing the source you just fetched so its heuristic fallback has something
to scan:
```
mcp__plugin_insightops-buddy_opsbuddy-git-ops__get_repo_mapping(
  source_path="<source_path>", job_id="<job_id>", source_content="<the source you just fetched>")
```
Check `resolution_method` in the response (`databricks_repos` / `job_git_source` /
`heuristic_source_scan`) — if it's the heuristic one, note that explicitly in your Step 4 report:
it's a strong signal, not a guarantee. If that tool isn't available, fall back to
`databricks-job-lineage`'s own `get_repo_mapping` (MCP) or `get-repo-mapping` (Bash) — neither has
a built-in heuristic, so if it errors, do the same scan yourself: `get_repo_mapping` only knows
Databricks' two *official* git-linkage mechanisms (a Repos checkout, or a job-level Git source) —
plenty of real jobs use neither, instead running a plain `git clone <url>` inside their own task
code, invisible to Databricks' APIs (confirmed in practice, twice, building this skill). Scan the
source you already fetched for a hardcoded git URL (a `git clone` call, or a bare
`https://.../....git` string, often assigned to a variable like `REPO_URL`) and use the first
match. Strip any embedded credential from that URL before using it further (e.g.
`https://TOKEN@github.com/...` → drop the `TOKEN@` — clone/read access shouldn't need it if the
repo's public, and this skill has no write step of its own anyway), and flag that embedded
credential in your Step 4 report as a live, exposed secret regardless of whether it's related to
the actual failure.

---

## Step 2 — Classify the Error

| Error category | How to identify | Typically `CODE_FIX_POSSIBLE` |
|---|---|---|
| Schema Mismatch | `AnalysisException`, `cannot resolve column`, schema evolution errors | true |
| Out-of-Memory (OOM) / Executor Lost | `ExecutorLostFailure`, `java.lang.OutOfMemoryError`, `Container killed by YARN` | false (unless caused by an obvious unbounded collect/join in code) |
| Null Pointer / NoneType | `NullPointerException`, `NoneType has no attribute` | true |
| Syntax Error | `SyntaxError`, `IndentationError`, `ParseException` | true |
| Permission / Access Denied | `AccessDeniedException`, `PERMISSION_DENIED`, `403` | false |
| Data Not Found at Source | `FileNotFoundException`, `Path does not exist`, empty source partition | false |
| Cluster Timeout / Startup Failure | `Cluster did not start`, `INSTANCE_UNREACHABLE`, spot eviction | false |
| Dependency / Library Import Error | `ModuleNotFoundError`, `ImportError`, library install failure | true |
| Data Skew / Partition Explosion | task duration outliers, `TooManyPartitionsException` | true (if code-level partitioning fix applies) |
| Upstream Task Dependency Failure | task `state.result_state == UPSTREAM_FAILED` | false (fix belongs in the upstream job) |
| Infrastructure / Cloud Provider Error | `InternalError`, cloud provider 5xx, network errors | false |

Pick the single best-matching category from the stack trace signature. This is a
**preliminary** classification — Step 3's agents may confirm or override it.

---

## Step 3 — Invoke root-cause-analysis (Cat L) — Adversarial Double-Check

`CODE_FIX_POSSIBLE` gates whether opsbuddy-fix is allowed to push a code change — one LLM
judgment call is not enough of a guardrail. Spawn **two independent instances** of the
`root-cause-analysis` subagent (Cat L) in parallel, each given identical input (full stack
trace, error message, task parameters, the Step 2 preliminary category and guess, the task's
actual source fetched in Step 1, and the resolved repo URL if Step 1 found one) but **no
visibility into each other's answer**. If a repo URL was resolved, tell each agent to clone it
themselves (they have Bash) to read the real project source, not just the one file already
fetched — that's what actually finds the specific bug, not just the wrapper exception.

Each returns:
```
ERROR_CATEGORY: <one of the 11 standardized categories>
ROOT_CAUSE_SUMMARY: <2-4 sentences>
CODE_FIX_POSSIBLE: <true|false>
AFFECTED_FILES: <comma-separated repo-relative paths, or "none">
SUGGESTED_FIX_APPROACH: <concrete, minimal, one-paragraph plan>
CONFIDENCE: <high|medium|low>
```

**Reconcile:**

| Agreement | Action |
|---|---|
| Both `true`, same category | Proceed with either verdict's `AFFECTED_FILES`/`SUGGESTED_FIX_APPROACH` (prefer higher `CONFIDENCE` if they differ in detail) |
| Both `false` | Proceed to halt — agreement on "not fixable" is just as actionable as agreement on "fixable" |
| **Disagree** on `CODE_FIX_POSSIBLE` | **Fail closed**: treat as `false`. Surface both verdicts verbatim so a human sees exactly where they diverged. Never average, guess, or pick one arbitrarily. |
| Either reports `CONFIDENCE: low` | Surface that explicitly regardless of agreement |
| **One agent errors out entirely** (a tool denial, crash, or timeout — no verdict returned at all, not a low-confidence one) | This is **not** the same as agreement or disagreement — don't silently treat it as either. Confirmed in practice: a permission-classifier denial killed one of two parallel Bash-using agents mid-run while the other completed normally. First, **retry only the failed agent once** (the surviving one already has a real answer — no need to redo it). If the retry also errors: you now have exactly one real verdict, not two independent ones — that's a genuinely weaker guarantee than the plugin's own adversarial-double-check design promises, so **say so explicitly** in the report rather than presenting it as a normal reconciled result. Proceed on that single verdict only if it reports `CODE_FIX_POSSIBLE: true` **and** `CONFIDENCE: high` — anything less (low/medium confidence, or `false`) fails closed the same as the low-confidence/disagreement cases above. |

### No subagent-spawning tool available at all (not one erroring out — none present)

Different failure mode from the row above, and it needs different handling. Claude Desktop has
no subagent-spawning tool of any kind, so this isn't occasional — it's every single run on that
client. **A disclaimer alone is not enough here — confirmed in practice, at real cost:** a
Desktop-driven run explicitly said it was "checking whether joins have ambiguous column names,"
looked directly at the one model that actually had that exact bug, concluded it "looks
structurally sound," and moved on to a different, less-evidenced hypothesis (a missing
credential) that it rode all the way to `CODE_FIX_POSSIBLE: false` and a Gate 3.5 halt. It wasn't
that it lacked information — it looked at the right file for the right reason and still missed
the defect, because a single pass has no adversary forcing it to double back.

When no subagent tool exists at all, do this instead of a single pass straight to a verdict:

1. Do the normal investigation (fetch source, resolve the repo, read the actual model/macro
   files) exactly as Step 1 describes.
2. Form a preliminary verdict — but **do not report it yet**.
3. **Take a genuine second pass, adversarially, before finalizing anything.** Specifically:
   re-open every file you concluded "looks fine" or "structurally sound" and re-read it with the
   explicit goal of finding the opposite of your first conclusion. For each category you
   considered and ruled out, ask "what would this look like if it WERE the cause?" and check
   for that pattern specifically — don't just re-skim for a general impression a second time.
   If your preliminary verdict is anything other than a specific, confirmed code defect (e.g.
   it's an environment/credential hypothesis, or "no obvious cause found"), that is itself the
   signal to look harder at the code before accepting it, not a stopping point.
4. Only after that second pass, report the verdict — and always disclose that this substituted a
   self-adversarial second pass for genuine independent agents, exactly as before, but now also
   state explicitly whether the second pass changed the preliminary verdict or confirmed it. A
   verdict that survived genuine self-adversarial re-checking is more trustworthy than one that
   was never re-examined at all, even without a second independent mind.

This does not fully replace two truly independent agents — a single mind checking its own work
twice still shares whatever blind spot caused the miss the first time. But it closes the specific
gap observed in practice: a plausible-sounding alternative hypothesis winning out over a defect
that was already sitting in view.

---

## Step 4 — Report

Return the reconciled verdict block (or both verdicts plus the "disagree → fail closed" note),
plus a plain-English one-paragraph root-cause summary. If invoked standalone, also state whether
a manual fix or `opsbuddy-fix $ARGUMENTS` is the appropriate next step.
