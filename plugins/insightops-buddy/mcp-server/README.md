# opsbuddy-git-ops MCP Server

A small local MCP server that gives Claude Desktop the things it's missing to run the
`opsbuddy-fix` pipeline end-to-end on its own: a shell for git/lint/test operations, and a
verified GitHub API path for PR creation — bundled with this plugin specifically so the whole
pipeline is self-sufficient, rather than depending on a separately-configured `github` MCP server
whose exact tool contract was never verified against this skill's needs. Scoped to git plumbing,
static validation, and PR creation only — it is **not** a general-purpose remote-shell server, and
it never merges or closes a PR (this pipeline's whole design never merges its own PR).

It mirrors `workflow/git_workflow.py`'s `GitRepoManager`/`GitHubClient` (the Claude Code side of
this plugin) and the `testing` skill's static-validation step, so behavior stays the same
regardless of which client is driving it.

## 1. Install

**Nothing to do** — `.mcp.json` launches this server via `uv run --directory
${CLAUDE_PLUGIN_ROOT}/mcp-server server.py`. `uv` (not a bare venv) reads `pyproject.toml` and
provisions a fresh environment itself on first launch — including `black`/`isort`/`flake8`/
`pytest`, which `run_static_checks`/`run_pytest` shell out to by name. This matters specifically
because plugin installs get **re-synced from git periodically** (e.g. on toggle off/on) — a
manually-created `.venv` doesn't survive that (found this the hard way: it got wiped on a plugin
reload), whereas `uv run` just re-provisions from `pyproject.toml` again, every time, with no
manual step at all. The only prerequisite is `uv` itself being installed
(https://docs.astral.sh/uv/getting-started/installation/) and on `PATH`.

For local development outside the plugin (not required for normal plugin use):
```bash
cd plugins/insightops-buddy/mcp-server
uv run server.py
```

## 2. Configure environment variables

`.mcp.json` passes `GITHUB_TOKEN` and `OPSBUDDY_MCP_WORKDIR` through from your own shell
environment (`"${GITHUB_TOKEN}"` / `"${OPSBUDDY_MCP_WORKDIR}"`) — it does **not** hardcode them,
specifically so no token ever needs to live in a file that gets committed or pasted anywhere. Set
them once in your normal shell profile (or Windows user environment variables):

- `GITHUB_TOKEN` — optional, only needed for `git_clone`/`git_push` over HTTPS against a private
  repo; leave unset if your remotes already authenticate over SSH.
- `OPSBUDDY_MCP_WORKDIR` — optional (defaults to a `workdir/` folder created next to `server.py`
  if unset/empty) — every `repo_dir`/`target_dir` argument must resolve underneath it.

For standalone local testing outside the plugin (not required for normal plugin use), you can
instead `copy .env.example .env` and fill it in there.

## 3. Run it standalone to sanity-check it starts

```bash
uv run server.py
```

First run provisions the environment (installs ~46 packages, takes a few seconds); subsequent
runs are fast. You should see `opsbuddy-git-ops workdir: ...` on stderr and the process will sit
waiting on stdio — that's normal, it's not meant to print anything else until a client talks to
it. Ctrl-C to stop. If `git` isn't on PATH you'll get a `FATAL:` message instead of a silent hang.

## 4. Installed via the marketplace? Registration is automatic

This plugin's `.mcp.json` (at `plugins/insightops-buddy/.mcp.json` in this repo) already declares
`opsbuddy-git-ops` — once the plugin is installed (`/plugin install insightops-buddy@insightops-vuddy2`),
Claude Code starts this server itself; there's nothing to add to `claude_desktop_config.json`
manually. Confirm with `/mcp` that `opsbuddy-git-ops` is connected and its 17 tools are listed:
`git_clone`, `git_create_branch`, `git_status`, `git_commit`, `git_push`, `run_static_checks`,
`run_pytest`, `get_repo_mapping`, `create_pr`, `find_open_pr`, `post_slack_alert`, `log_incident`,
`read_file`, `write_file`, `get_job_run`, `get_latest_failed_run`, `trigger_job_run`.

**If you previously registered this server by hand** directly in `claude_desktop_config.json`
(e.g. while testing before this repo existed), remove that entry now — otherwise the same server
ends up registered twice, once as a bare `opsbuddy-git-ops` and once as this plugin's namespaced
`mcp__plugin_insightops-buddy_opsbuddy-git-ops__*`, which is confusing and redundant.

## What each tool does

| Tool | What it runs | Notes |
|---|---|---|
| `git_clone` | `git clone` | Refuses to clone into a non-empty directory or anywhere outside the workdir. |
| `git_create_branch` | `git checkout <base>`, `git pull origin <base>`, `git checkout -b <branch>` | Always branches from a freshly-pulled base. |
| `git_status` | `git branch --show-current`, `git status --porcelain` | Check before `git_commit` to confirm exactly what will be staged. |
| `git_commit` | `git add <files>`, `git commit -m <message>` | Returns the new commit SHA. |
| `git_push` | `git push -u <remote> <branch>` | Authenticated via `GIT_ASKPASS` if `GITHUB_TOKEN` is set — the token never touches `.git/config` or the URL. |
| `git_cleanup` | `shutil.rmtree` on a resolved `repo_dir` | The only delete capability anywhere on this server, and deliberately narrow — same `OPSBUDDY_MCP_WORKDIR` sandbox as every `git_*`/`read_file`/`write_file` tool. Closes a real gap: opsbuddy-fix clones a fresh checkout per incident (and again per retry), with no way to remove it afterward, so clones accumulated under the workdir indefinitely. Call once a PR has merged or the incident's abandoned. A `repo_dir` that's already gone returns `{"deleted": true}`, not an error — the requested end state already holds either way. |
| `run_static_checks` | `black --check`, `isort --check`, `flake8 --max-line-length=120`, `python -m py_compile`, then `pytest` on any matching `python/tests/test_<module>.py` | Mirrors the `testing` skill's Step 2 exactly; non-`.py` files in the list are reported as skipped, not silently dropped. |
| `run_pytest` | `pytest <test_path> -m <markers> -v` | For ad-hoc/retry test runs outside the fixed `test_<module>.py` convention. |
| `get_repo_mapping` | Databricks Repos list / job `git_source` lookup via `databricks-sdk`, or a regex scan of a passed-in `source_content` string | The only tool needing `DATABRICKS_HOST`/`DATABRICKS_TOKEN` — everything else works with neither set. Re-implements the same lookup `databricks-job-lineage`'s own `get_repo_mapping` does (so this plugin doesn't depend on that one being installed), plus a third fallback neither has: scanning already-fetched task source for a hardcoded git URL, for jobs whose task code clones a repo manually rather than using either official Databricks git-linkage mechanism. |
| `create_pr` | GitHub API `create_pull` via PyGithub | Requires `GITHUB_TOKEN` (unlike every `git_*` tool above, which can work without one for public repos/SSH remotes — the GitHub API always needs a token). Never merges or closes anything. |
| `find_open_pr` | GitHub API `get_pulls(state="open")` via PyGithub, filtered by title/branch substring | For Phase 4's PR-dedup check — reuse an existing PR for this incident instead of opening a duplicate. |
| `post_slack_alert` | Slack Web API `chat.postMessage` (preferred, if `SLACK_BOT_TOKEN`+`SLACK_CHANNEL_ID` set) or `POST` to `SLACK_WEBHOOK_URL` | Called up to five times per run, once per pipeline checkpoint (`stage="incident_detected"/"pr_opened"/"pr_merged"/"verification_running"/"resolved"`, each with its own header/emoji), plus a free-text `message` field for prose like the RCA summary or a verification result, and a `job_id` field (the job being fixed — distinct from `databricks_run_id`, a specific run of it). `stage`/`message`/`job_id` are all optional — omitting them reproduces the original single generic "incident summary" post. Pass a prior call's returned `ts` as `thread_ts` to reply into that message's thread instead of posting a new top-level one — only possible on the bot-token path; the webhook path has no message identity to thread into. Mirrors `workflow/slack_workflow.py`'s `send-incident-summary` exactly (same fields, same block layout, same options, same threading contract). |
| `log_incident` | `INSERT` into the Databricks ops incident-log table via `databricks-sdk`'s SQL Statement Execution API | Phase 10's incident-log write. Needs `DATABRICKS_HOST`/`DATABRICKS_TOKEN` (like `get_repo_mapping`) plus `DATABRICKS_SQL_WAREHOUSE_ID` — reuse the same warehouse ID already configured for the `databricks-lineage` plugin, if one is registered, rather than a new credential. `record`'s keys must match the real table's actual columns exactly — see the `opsbuddy-fix` skill's Phase 10 section for the verified shape. Insert-only, matching the CLI it mirrors. |
| `read_file` | Read a text file at `path` under an already-cloned `repo_dir` | Whole-file only, UTF-8 text only. |
| `write_file` | Overwrite (or create) a text file at `path` under `repo_dir` with the given content | Whole-file only — read, edit in full, write back. The one Phase 5 (remediation) needs on Claude Desktop, which has no file-editing tool of its own — confirmed in practice: without this, a Desktop-driven run got as far as creating the hotfix branch and then had no way to actually write the fix. |
| `get_job_run` | Databricks `jobs.get_run`/`jobs.get_run_output` via `databricks-sdk` | Phase 1's telemetry fetch. Needs `DATABRICKS_HOST`/`DATABRICKS_TOKEN` (like `get_repo_mapping`). |
| `get_latest_failed_run` | Databricks `jobs.list_runs`, filtered to the most recent failed/timed-out/canceled run | For resolving a run ID when only a job ID is known. |
| `trigger_job_run` | Databricks `jobs.run_now`, then polls `jobs.get_run` until a terminal state | Gate 8.5's real-verification re-run. Gated on `OPSBUDDY_VERIFY_ALLOWLIST` (comma-separated job IDs, or `"all"`) — without it, refuses unless `force=true` is passed, which should only happen after a human has explicitly approved that specific re-run. Blocks for up to `timeout_seconds` (default 600s) — same blocking behavior as the CLI it mirrors. |
| `get_table_lineage` | Unity Catalog system table `system.access.table_lineage` via a SQL warehouse | Real data lineage — which tables a run read from and wrote to, one hop of `upstream_producers` (what wrote the tables it read — check this if the real root cause is bad data from further back, not this job's own code), and one hop of `downstream_consumers` (what reads the tables it wrote). Distinct from `get_job_run`'s "downstream impact," which only ever looks at task state inside one job's own DAG, never at tables. Needs `DATABRICKS_SQL_WAREHOUSE_ID` (shared with `log_incident`) plus Unity Catalog lineage tracking enabled on the workspace. |
| `get_incident_history` | `SELECT` against the same incident-log table `log_incident` writes to, via a SQL warehouse | Has this job failed before — or is a whole error category breaking multiple jobs at once? `log_incident` was insert-only until this tool existed — nothing ever read it back, so every failure was diagnosed as a first-time event even on a job that's failed the same way repeatedly. Requires at least one of `job_id`/`error_category`; leaving `job_id` empty turns it into a cross-job query (e.g. "has any job hit Schema Mismatch in the last 7 days"), bounded by `days` (default 30). Returns `is_recurring: true` once 2+ matches exist and `distinct_jobs_affected` (the real platform-wide signal — 5 hits on one job isn't the same story as 5 hits spread across 5 jobs). A signal to surface prominently, never a shortcut to skip re-diagnosis and replay an old fix blind. Needs `DATABRICKS_SQL_WAREHOUSE_ID` (same as `log_incident`/`get_table_lineage`). |

Behind a TLS-intercepting corporate proxy (e.g. Zscaler), `create_pr`/`find_open_pr` need one more
thing: `OPSBUDDY_EXTRA_CA_CERT` (or it reuses `NODE_EXTRA_CA_CERTS` automatically if that's already
set for another MCP server) pointing at your proxy's root CA cert. Without it, both tools fail
outright with `SSLCertVerificationError` — confirmed in practice, not a hypothetical — even with a
correct `GITHUB_TOKEN`. The server builds a combined bundle (public CAs + that cert) and points
`REQUESTS_CA_BUNDLE` at it automatically; no manual bundle-building needed.

`post_slack_alert`/`log_incident` were both added and verified end-to-end (a real Slack post, a
real incident-log insert followed by a manual cleanup delete) after a real Desktop-driven run
correctly reported both as unavailable rather than fake having done them — the honest report is
what surfaced this as a gap worth closing, not a guess.

Every tool that touches a SQL warehouse (`log_incident`, `get_table_lineage`,
`get_incident_history`) shares one execution helper that polls a statement's status every 2s
instead of blocking on one long `wait_timeout` call. This isn't cosmetic: a stopped/auto-
suspended SQL warehouse commonly takes 30-60s+ to wake up, and a single call blocking that long
risks exceeding the *caller's* own timeout — confirmed in practice, `log_incident` timed out
this exact way on a real run (indistinguishable, from the caller's side, from the server being
unresponsive), even though the insert would have succeeded once the warehouse finished starting.

## Safety model

- **Path sandboxing**: every `repo_dir`/`target_dir` must resolve underneath `OPSBUDDY_MCP_WORKDIR`
  (checked via `Path.relative_to`, not a string prefix check) — a path that escapes it is refused
  before any git/subprocess call runs, not caught after the fact.
- **No arbitrary command execution**: there is no generic "run this shell command" tool. Every
  tool is a fixed, specific git or lint/test invocation with an explicit argument list
  (`subprocess.run([...])`, never `shell=True`), so tool arguments can't inject extra flags or
  commands.
- **Bounded runtime**: every subprocess has a timeout (`OPSBUDDY_MCP_TIMEOUT_SECONDS`, default
  300s) so a hung test run or an infinite loop in a bad fix can't hang the MCP connection forever.
- Every tool returns an `error` (or per-check `passed`) field instead of raising — same philosophy
  as the `databricks-lineage` server: callers get a structured verdict, not a stack trace.

## Running as a remote connector

Same pattern as the `databricks-lineage` server — set `MCP_TRANSPORT=http`, `MCP_HOST`,
`MCP_PORT`, and `MCP_API_KEY`, then run `uv run server.py`. Given this server can write to a real
working tree and push to a real remote, think harder before exposing it this way than you would
for a read-only server — put a reverse proxy/TLS in front, and treat `MCP_API_KEY` as sensitive
as `GITHUB_TOKEN` itself, since anyone holding it can push commits as this process.
