# opsbuddy-git-ops MCP Server

A small local MCP server that gives Claude Desktop the one thing it's missing to run the
`opsbuddy-fix` pipeline: a shell. Jira already has an MCP path (the Atlassian connector), and
GitHub/Slack/Databricks already have MCP servers configured — none of those can clone a repo,
run `git commit`/`push` against a local working tree, or shell out to `black`/`isort`/`flake8`/
`pytest`. This server exposes exactly that, scoped to git plumbing and static validation — it is
**not** a general-purpose remote-shell server.

It mirrors `workflow/git_workflow.py`'s `GitRepoManager` (the Claude Code side of this plugin)
and the `testing` skill's static-validation step, so behavior stays the same regardless of which
client is driving it.

## 1. Install

**Required even when this plugin is installed via the marketplace** — `.mcp.json` declares how to
*launch* this server (`${CLAUDE_PLUGIN_ROOT}/mcp-server/.venv/Scripts/python.exe`), but installing
the plugin does not create that venv or install its dependencies for you. Do this once, in the
installed plugin's own directory (find it with `/plugin` in Claude Code, or see this repo's
`plugins/insightops-buddy/mcp-server/` if you're developing locally):

```bash
cd plugins/insightops-buddy/mcp-server
python -m venv .venv && .venv\Scripts\activate   # or: source .venv/bin/activate
pip install -r requirements.txt
```

`black`/`isort`/`flake8`/`pytest` must be importable/runnable in this same environment —
`run_static_checks`/`run_pytest` shell out to them by name, they aren't Python imports.

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
python server.py
```

You should see `opsbuddy-git-ops workdir: ...` on stderr and the process will sit waiting on
stdio — that's normal, it's not meant to print anything else until a client talks to it. Ctrl-C
to stop. If `git` isn't on PATH you'll get a `FATAL:` message instead of a silent hang.

## 4. Installed via the marketplace? Registration is automatic

This plugin's `.mcp.json` (at `plugins/insightops-buddy/.mcp.json` in this repo) already declares
`opsbuddy-git-ops` — once the plugin is installed (`/plugin install insightops-buddy@insightops-vuddy`),
Claude Code starts this server itself; there's nothing to add to `claude_desktop_config.json`
manually. Confirm with `/mcp` that `opsbuddy-git-ops` is connected and its 7 tools are listed:
`git_clone`, `git_create_branch`, `git_status`, `git_commit`, `git_push`, `run_static_checks`,
`run_pytest`.

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
| `run_static_checks` | `black --check`, `isort --check`, `flake8 --max-line-length=120`, `python -m py_compile`, then `pytest` on any matching `python/tests/test_<module>.py` | Mirrors the `testing` skill's Step 2 exactly; non-`.py` files in the list are reported as skipped, not silently dropped. |
| `run_pytest` | `pytest <test_path> -m <markers> -v` | For ad-hoc/retry test runs outside the fixed `test_<module>.py` convention. |

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
`MCP_PORT`, and `MCP_API_KEY`, then run `python server.py`. Given this server can write to a real
working tree and push to a real remote, think harder before exposing it this way than you would
for a read-only server — put a reverse proxy/TLS in front, and treat `MCP_API_KEY` as sensitive
as `GITHUB_TOKEN` itself, since anyone holding it can push commits as this process.
