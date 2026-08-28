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
`opsbuddy-git-ops` — once the plugin is installed (`/plugin install insightops-buddy@insightops-vuddy`),
Claude Code starts this server itself; there's nothing to add to `claude_desktop_config.json`
manually. Confirm with `/mcp` that `opsbuddy-git-ops` is connected and its 12 tools are listed:
`git_clone`, `git_create_branch`, `git_status`, `git_commit`, `git_push`, `run_static_checks`,
`run_pytest`, `get_repo_mapping`, `create_pr`, `find_open_pr`, `read_file`, `write_file`.

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
| `get_repo_mapping` | Databricks Repos list / job `git_source` lookup via `databricks-sdk`, or a regex scan of a passed-in `source_content` string | The only tool needing `DATABRICKS_HOST`/`DATABRICKS_TOKEN` — everything else works with neither set. Re-implements the same lookup `databricks-job-lineage`'s own `get_repo_mapping` does (so this plugin doesn't depend on that one being installed), plus a third fallback neither has: scanning already-fetched task source for a hardcoded git URL, for jobs whose task code clones a repo manually rather than using either official Databricks git-linkage mechanism. |
| `create_pr` | GitHub API `create_pull` via PyGithub | Requires `GITHUB_TOKEN` (unlike every `git_*` tool above, which can work without one for public repos/SSH remotes — the GitHub API always needs a token). Never merges or closes anything. |
| `find_open_pr` | GitHub API `get_pulls(state="open")` via PyGithub, filtered by title/branch substring | For Phase 4's PR-dedup check — reuse an existing PR for this incident instead of opening a duplicate. |
| `read_file` | Read a text file at `path` under an already-cloned `repo_dir` | Whole-file only, UTF-8 text only. |
| `write_file` | Overwrite (or create) a text file at `path` under `repo_dir` with the given content | Whole-file only — read, edit in full, write back. The one Phase 5 (remediation) needs on Claude Desktop, which has no file-editing tool of its own — confirmed in practice: without this, a Desktop-driven run got as far as creating the hotfix branch and then had no way to actually write the fix. |

Behind a TLS-intercepting corporate proxy (e.g. Zscaler), `create_pr`/`find_open_pr` need one more
thing: `OPSBUDDY_EXTRA_CA_CERT` (or it reuses `NODE_EXTRA_CA_CERTS` automatically if that's already
set for another MCP server) pointing at your proxy's root CA cert. Without it, both tools fail
outright with `SSLCertVerificationError` — confirmed in practice, not a hypothetical — even with a
correct `GITHUB_TOKEN`. The server builds a combined bundle (public CAs + that cert) and points
`REQUESTS_CA_BUNDLE` at it automatically; no manual bundle-building needed.

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
