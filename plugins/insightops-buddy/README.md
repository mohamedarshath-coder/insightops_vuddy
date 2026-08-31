# insightops-buddy

A Claude Code plugin that packages the `opsbuddy-fix` incident-response pipeline: skills, a
subagent, bundled `workflow/` CLI scripts, and a bundled MCP server (`mcp-server/`), all wired
together so the plugin works the same way regardless of which client installs it.

## What it does

When a Databricks job fails in production, `/insightops-buddy:opsbuddy-fix <run-id>` handles it
automatically:

1. **Diagnoses why it failed** — reads the error and the actual source code, double-checked by
   two independent AI passes so one bad guess can't push a wrong fix.
2. **Decides if it's actually fixable in code** — if it's an infra/data problem instead, it stops
   and flags it for a human rather than forcing a fake fix.
3. **Files a Jira ticket** for the incident (with dedup — won't double-file for the same run) and
   drives it through a real Kanban lifecycle — **To Do → In Progress → In Review → Done** — not
   just a single creation call, so the board always reflects where the fix actually stands.
4. **Writes the fix, opens a PR** on GitHub (with dedup — reuses an existing open PR if one
   already covers this run).
5. **Reviews its own fix** against the confirmed root cause before anyone else looks at it.
6. **Optionally re-runs the job for real** to prove the fix actually works — only for jobs
   explicitly allow-listed for auto-verification, or with a person's explicit approval, and only
   when the job's git-linkage actually supports pointing it at an unmerged branch.
7. **Posts five Slack checkpoints across the run**, not one final message — incident detected
   (with the plain-English root cause), PR opened (not yet merged), PR merged, verification
   running, and resolved — so a channel reads as a timeline of what's happening, not a single
   "done" ping at the end.
8. **Logs the incident to a Databricks table and publishes an incident postmortem page to
   Confluence** — idempotent by title, so re-runs update the same page rather than duplicating it.

A person still reviews and merges the PR — this plugin never merges anything on its own.

## Install

```
/plugin marketplace add mohamedarshath-coder/insightops_vuddy
/plugin install insightops-buddy@insightops-vuddy
```

Then run the one-time MCP server setup in `mcp-server/README.md` (create its venv, `pip install
-r requirements.txt`) — installing the plugin does not do this for you, since Claude Code only
declares *how* to launch the server (`.mcp.json`), not its Python dependencies.

## Environment variables

`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, `GITHUB_TOKEN`, `GITHUB_REPO` (default only — every
git/PR command also accepts an explicit `--repo`), `JIRA_BASE_URL`, `JIRA_EMAIL`,
`JIRA_API_TOKEN`, `SLACK_WEBHOOK_URL`. Optional: `JIRA_OPS_PROJECT_KEY` (default `OPS` — pass
your real project key if your Jira instance doesn't have one), `DATABRICKS_OPS_INCIDENT_TABLE`,
`DATABRICKS_HTTP_PATH`, `OPSBUDDY_GIT_AUTHOR_NAME`/`OPSBUDDY_GIT_AUTHOR_EMAIL`,
`OPSBUDDY_VERIFY_ALLOWLIST` (comma-separated job IDs, or `all`, gating Gate 8.5's real job
re-run), `OPSBUDDY_MCP_WORKDIR` (the bundled MCP server's sandboxed clone directory).

**Confluence (Phase 10.5), all optional:** `CONFLUENCE_BASE_URL` (defaults to
`<JIRA_BASE_URL>/wiki` — Confluence Cloud's standard path on the same Atlassian site, so usually
nothing to set), `CONFLUENCE_EMAIL`/`CONFLUENCE_API_TOKEN` (default to `JIRA_EMAIL`/
`JIRA_API_TOKEN` — one Atlassian API token normally covers both Jira and Confluence on the same
account), `CONFLUENCE_SPACE_KEY` (default `OOP`), `CONFLUENCE_AUTHOR` (default `opsbuddy-fix`,
shown on the published page).

## Structure

```
insightops-buddy/
├── .claude-plugin/plugin.json      # plugin manifest
├── .mcp.json                       # declares the bundled opsbuddy-git-ops MCP server
├── skills/
│   ├── opsbuddy-fix/SKILL.md       # the 11-phase orchestrator + Phase 10.5 Confluence
│   ├── databricks-debug/SKILL.md   # telemetry + 11-category classification sub-skill
│   ├── testing/SKILL.md            # static validation sub-skill
│   └── pr-review-opsbuddy-fix/SKILL.md   # Mode A automated PR review
├── agents/root-cause-analysis.md   # Cat L root-cause subagent
├── workflow/                       # bundled CLI scripts (databricks/jira/git/slack/confluence)
├── python/utils/                   # bundled shared helpers (config, logger, databricks_conn)
└── mcp-server/                     # bundled MCP server (git ops + static validation) — see its README
```

## MCP tool naming

A plugin's own bundled MCP server is namespaced as
`mcp__plugin_<plugin-name>_<server-name>__<tool-name>` — this plugin's bundled server's tools are
therefore `mcp__plugin_insightops-buddy_opsbuddy-git-ops__*`, not the bare
`mcp__opsbuddy-git-ops__*` you'd get from registering the same server directly in a client's own
MCP config outside a plugin. See each skill's own MCP-preferred/Bash-fallback blocks for exact
tool calls, and `mcp-server/README.md`'s safety model for what that server will and won't do.

## Known limitations (found in practice, not theoretical)

- Gate 8.5's real re-run only works cleanly for jobs checked out via **Databricks Repos**. For
  jobs using a **job-level Git source**, there's no bundled one-liner yet — the skill documents
  the manual SDK steps (temporarily repoint `git_source.git_branch`, run, restore).
- Some jobs' own notebook/task code clones its dependencies itself, independent of the job's git
  linkage — a re-run in that case can silently ignore the hotfix branch entirely. Gate 8.5 treats
  that as **inconclusive**, not a fix failure.
- A single failed run can hide more than one independent bug — the pipeline fixes what both
  root-cause-analysis passes agree is safely fixable and flags the rest for manual follow-up
  rather than forcing one verdict to cover everything.
- PR creation (Phase 7) and the Databricks incident-log write (Phase 10) have no MCP path yet —
  both stay Bash-only regardless of client.
- The Slack MCP-preferred call (`slack_post_message`) documents a tool name/signature that has
  never actually been verified against a live Slack MCP server in development — confirm it
  against your installed server before trusting it.
- The `databricks-lineage` MCP tool names referenced in these skills assume that server ships from
  a plugin named `databricks-job-lineage` — that's inferred from directory layout, not verified,
  since it's a separate, private repo. Confirm with `/mcp` once installed and adjust if it differs.
