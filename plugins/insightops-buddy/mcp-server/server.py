"""
opsbuddy-git-ops MCP Server
===========================

Fills the one real gap left when running the opsbuddy-fix pipeline from Claude Desktop instead
of Claude Code: Desktop has no Bash tool, so it can't clone a repo, run `git` commands, or shell
out to lint/test tools. Jira already has an MCP path (the Atlassian connector), and GitHub/
Slack/Databricks already have MCP servers -- none of those cover local git plumbing or running
black/isort/flake8/pytest against a working tree. This server exposes exactly that, and nothing
else: it is not a general-purpose shell-exec server.

Tools:
    git_clone(repo_url, target_dir)
    git_create_branch(repo_dir, branch, base="main")
    git_status(repo_dir)
    git_commit(repo_dir, message, files)
    git_push(repo_dir, branch, remote="origin")
    run_static_checks(repo_dir, files)
    run_pytest(repo_dir, test_path, markers="not integration")
    get_repo_mapping(source_path, job_id="", source_content="")
    create_pr(repo, branch, base, title, body)
    find_open_pr(repo, search_text)
    read_file(repo_dir, path)
    write_file(repo_dir, path, content)
    post_slack_alert(jira_ticket_id, job_id, databricks_run_id, error_category, pr_url, pr_review_verdict, execution_status, stage, message, thread_ts)
    log_incident(record)
    get_job_run(run_id)
    get_latest_failed_run(job_id)
    trigger_job_run(job_id, timeout_seconds=600, force=False)
    get_table_lineage(run_id)

get_table_lineage is real Unity Catalog data lineage (which tables a run read/wrote, one hop of
upstream producers of what it read, and one hop of downstream consumers of what it wrote) --
distinct from get_job_run's "downstream impact", which only ever looks at task state inside one
job's own DAG, not tables. upstream_producers is what to check when the real root cause might be
bad data from further back in the pipeline, not a bug in the job that's actually failing. This
plugin had no data lineage capability at all until this tool was added; the only prior source of
it (databricks-job-lineage's own get_table_lineage) lived in a plugin this one was deliberately
built not to depend on, and that version was one-directional (downstream only). Needs
DATABRICKS_SQL_WAREHOUSE_ID (same var log_incident already uses) plus Unity Catalog lineage
tracking enabled on the workspace -- see get_table_lineage's own docstring.

get_job_run/get_latest_failed_run/trigger_job_run close the last two Bash-only steps in the whole
pipeline: Phase 1's telemetry fetch and Gate 8.5's real-verification re-run had no MCP path in
THIS plugin at all -- only the separate databricks-job-lineage plugin covered similar ground
(get_job_run, get_latest_failed_run, trigger_job_run gated behind its own
DATABRICKS_ALLOW_JOB_TRIGGER), which this plugin was never meant to depend on for the same reason
get_repo_mapping/create_pr aren't either: that plugin's tool contract isn't verified against this
skill's needs, and it might not even be installed. Confirmed in practice: without an MCP path,
Gate 8.5's real re-run on a live Desktop-driven run had to fall back to whatever bash-like sandbox
Desktop has for other tasks -- a different, less-tested execution environment than this server,
potentially without the same local env/credentials. These three mirror
workflow/databricks_workflow.py's DatabricksClient exactly (same telemetry shape, same
OPSBUDDY_VERIFY_ALLOWLIST/force safety gate on trigger_job_run) so behavior stays identical to the
Bash path -- Bash is now a true last-resort fallback for every phase, not a silent primary path.

read_file/write_file close the one phase that had no Desktop-side fallback at all: opsbuddy-fix's
Phase 5 (remediation) needs to actually edit a file in the cloned working tree, and unlike every
other phase, Desktop has no Bash tool and no file-editing tool of its own to fall back to -- every
one of its capabilities comes from an MCP server. Confirmed in practice: a real Desktop-driven run
diagnosed the fix correctly, created the hotfix branch, and then had nothing to write the change
with -- it had to halt and hand off to a human for a one-line edit. These two tools are Claude
Code's Read/Edit, exposed over MCP, scoped the same way every other tool here is (must resolve
under the repo's own working tree, whole-file only, no shell involved).

post_slack_alert/log_incident close Phase 10's Desktop-side gap the same way: that phase's Slack
alert and Databricks incident-log write had only ever had a Bash fallback (workflow/slack_workflow.py,
workflow/databricks_workflow.py log-incident) -- Desktop has no Bash, so a Desktop-driven run could
report Phase 10 as done and silently mean "nothing was actually sent or logged." Confirmed in
practice: a real Desktop-driven multi-bug run got all the way through PR creation and review, then
had to report both alerting and incident-logging as unavailable rather than fake having done them --
which is the right call for a tool gap, but still a gap. These two mirror workflow/slack_workflow.py's
SlackClient and python/utils/databricks_conn.py's insert_ops_incident_log exactly (same payload
shape, same SQL construction) so behavior stays identical to the Code-driven path.

get_repo_mapping needs DATABRICKS_HOST/DATABRICKS_TOKEN (only that one tool -- everything else
above works with zero Databricks config at all). It re-implements the same Databricks Repos /
job-level Git source lookup the databricks-job-lineage plugin's own get_repo_mapping does, so this
plugin doesn't depend on that other plugin being installed or exposing that tool, PLUS a third
fallback neither of those has: scanning already-fetched task source for a hardcoded git URL, for
jobs whose task code clones a repo manually in Python rather than using either of Databricks'
official git-linkage mechanisms (confirmed in practice, twice, building this plugin).

create_pr/find_open_pr talk to the GitHub API directly (via PyGithub), unlike every git_* tool
above (which only ever runs local `git` CLI commands) -- mirrors workflow/git_workflow.py's
GitHubClient from the Claude Code side of this plugin, so opsbuddy-fix's Phase 7 (PR creation) and
Phase 4 (PR dedup) have a real MCP-preferred path bundled with this plugin, instead of depending on
a separately-configured `github` MCP server whose exact tool contract was never verified against
this skill's needs. This server still never merges or closes anything -- PR creation only.

Every path argument (target_dir, repo_dir) must resolve underneath OPSBUDDY_MCP_WORKDIR (default:
a workdir/ folder next to this file) -- this server refuses to touch anything outside it, so a
bad or malicious argument can't walk it into unrelated parts of the filesystem.

Auth: GITHUB_TOKEN -- optional for the git_* tools (only needed for HTTPS clone/push against a
private repo; SSH remotes need nothing from this server), but REQUIRED for create_pr/find_open_pr
(the GitHub API always needs a token, even for public repos).

Run it:
    pip install -r requirements.txt
    export GITHUB_TOKEN=ghp_...        # optional, HTTPS clones/pushes of private repos only
    export OPSBUDDY_MCP_WORKDIR=D:\opsbuddy\opsbuddy-git-workdir   # optional, see default below
    python server.py

Then point an MCP client (Claude Desktop, Claude Code, etc.) at it as a stdio server -- see
README.md for the exact client config.
"""

import os
import re
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP


def _load_dotenv(path: str) -> None:
    """Minimal stdlib-only ".env" loader -- this server is launched via `uv run` (see
    .mcp.json), which does not auto-load a .env file on its own, and this file never called
    python-dotenv itself despite an .env.example existing right next to it. Populates
    os.environ from KEY=VALUE lines in `path` for any key that isn't already set to a real
    (non-empty) value -- a genuinely-exported var, or one an MCP client's own env block
    supplies (even as an empty string from an unresolved ${VAR}), still gets filled in from
    here rather than silently staying blank. Silently does nothing if the file doesn't exist.
    """
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # A blank .env line (KEY=, no value -- what .env.example's optional vars look like)
            # must NOT get set into os.environ at all: several config reads here do
            # int(os.environ.get(KEY, "300")) and expect a genuinely-missing key to fall through
            # to that default, not "" (which int() rejects) -- confirmed in practice, this broke
            # OPSBUDDY_MCP_TIMEOUT_SECONDS the first time this loader ran against the real
            # .env.example. An already-set real (non-empty) value always wins either way.
            if value and not os.environ.get(key):
                os.environ[key] = value


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# Only get_repo_mapping needs these -- lazily validated inside that tool, not at startup, so
# every other tool in this server keeps working with zero Databricks config at all.
DATABRICKS_HOST = os.environ.get("DATABRICKS_HOST", "").strip()
DATABRICKS_TOKEN = os.environ.get("DATABRICKS_TOKEN", "").strip()

# Only create_pr/find_open_pr need this -- an extra corporate-proxy root CA (e.g. Zscaler) to
# trust, on top of the normal public CA bundle. Confirmed in practice: behind a TLS-intercepting
# corporate proxy, PyGithub's underlying `requests` calls fail outright with
# SSLCertVerificationError without this, even though GITHUB_TOKEN and everything else is correct.
# Reuses NODE_EXTRA_CA_CERTS if that's already set for another MCP server on this machine (the
# official "github" MCP server needs the same cert for the same reason) -- OPSBUDDY_EXTRA_CA_CERT
# is the primary name, for when this server is the only one that needs it configured.
EXTRA_CA_CERT = (
    os.environ.get("OPSBUDDY_EXTRA_CA_CERT", "").strip()
    or os.environ.get("NODE_EXTRA_CA_CERTS", "").strip()
)

# Only post_slack_alert needs these -- every other tool works with them unset.
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
# Optional, preferred over the webhook above when both are set: only the Slack Web API's
# chat.postMessage (bot-token path) can thread a reply under an earlier message, since an
# incoming webhook has no way to return the posted message's identity for a later call to reply
# into. Needs a Slack app with a bot token (xoxb-...) invited into SLACK_CHANNEL_ID.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "").strip()
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "").strip()

# Only log_incident needs these -- every other tool works with them unset. The warehouse ID is
# the same value already used by the databricks-lineage plugin's DATABRICKS_SQL_WAREHOUSE_ID (if
# that's registered on this machine), not a new credential -- reuse it rather than configuring a
# second one. The table name mirrors python/utils/databricks_conn.py's own default exactly.
DATABRICKS_SQL_WAREHOUSE_ID = os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID", "").strip()
DATABRICKS_OPS_INCIDENT_TABLE = os.environ.get(
    "DATABRICKS_OPS_INCIDENT_TABLE", "dev.ops_incidents.incident_log"
).strip()

# Every clone/repo-dir argument must resolve under this directory -- the one guardrail that
# keeps a bad or malicious path from writing/deleting outside a known sandbox. Defaults to a
# folder next to this script so `python server.py` works with zero required config.
WORKDIR = Path(
    os.environ.get("OPSBUDDY_MCP_WORKDIR", str(Path(__file__).resolve().parent / "workdir"))
).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

# How long a single static-check or test subprocess is allowed to run before this server kills
# it -- a hung `pytest` (or a fix that introduced an infinite loop) shouldn't hang the MCP
# connection forever.
SUBPROCESS_TIMEOUT_SECONDS = int(os.environ.get("OPSBUDDY_MCP_TIMEOUT_SECONDS", "300"))

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0").strip()
MCP_PORT = int(os.environ.get("MCP_PORT", "8001"))
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()

try:
    subprocess.run(["git", "--version"], capture_output=True, check=True)
except Exception as exc:  # noqa: BLE001 - startup diagnostic, not a tool call
    print(f"FATAL: `git` is not on PATH or failed to run: {exc}", file=sys.stderr)
    sys.exit(1)

if not GITHUB_TOKEN:
    print(
        "WARNING: GITHUB_TOKEN is not set -- git_clone/git_push will only work against repos "
        "reachable without HTTPS token auth (e.g. already-configured SSH remotes, or public "
        "repos for clone).",
        file=sys.stderr,
    )

print(f"opsbuddy-git-ops workdir: {WORKDIR}", file=sys.stderr)

mcp = FastMCP("opsbuddy-git-ops", host=MCP_HOST, port=MCP_PORT)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _resolve_under_workdir(raw_path: str, *, must_exist: bool = False) -> Path:
    """Resolve `raw_path` (absolute or relative to WORKDIR) and refuse anything outside it."""
    candidate = Path(raw_path)
    resolved = (candidate if candidate.is_absolute() else WORKDIR / candidate).resolve()
    try:
        resolved.relative_to(WORKDIR)
    except ValueError:
        raise ValueError(
            f"{raw_path!r} resolves to {resolved}, which is outside the allowed workdir "
            f"{WORKDIR}. Pass a path under the workdir (or set OPSBUDDY_MCP_WORKDIR to widen it)."
        )
    if must_exist and not resolved.exists():
        raise ValueError(f"{resolved} does not exist")
    return resolved


def _resolve_repo_relative(repo_dir: str, rel_path: str) -> Path:
    """Resolve `rel_path` against an already-cloned `repo_dir` and refuse anything that would
    land outside that repo's own working tree (a `../` escape) or inside `.git/` (repo internals,
    never a source file a fix should touch). Reuses `_resolve_under_workdir` first so `repo_dir`
    itself still has to be a real, already-existing checkout under the server's sandboxed
    workdir -- this adds a second, narrower boundary on top of that: the repo root itself."""
    repo = _resolve_under_workdir(repo_dir, must_exist=True)
    candidate = (repo / rel_path).resolve()
    try:
        relative = candidate.relative_to(repo)
    except ValueError:
        raise ValueError(f"{rel_path!r} resolves outside repo_dir {repo} -- refusing")
    if relative.parts and relative.parts[0] == ".git":
        raise ValueError(f"{rel_path!r} is inside .git/ -- refusing to touch repo internals")
    return candidate


def _kill_process_tree(pid: int) -> None:
    """Best-effort kill of a process AND its children. A plain .kill()/.terminate() only
    signals the immediate process -- git and lint/test tools can spawn helper subprocesses
    that survive that, leaving a "timed out" tool call quietly orphaned in the background
    forever. `taskkill /T` (Windows) / process-group SIGKILL (POSIX) kills the whole tree."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10
            )
        else:
            import signal

            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - best-effort cleanup, never let this raise
        pass


def _run(args: List[str], cwd: Path, env: Optional[dict] = None, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """subprocess.run-alike used for every git/lint/test invocation in this server.

    Two things a plain `subprocess.run(..., timeout=...)` does NOT reliably give you, both
    confirmed the hard way in practice (a git_clone call left an orphaned `git.exe` running
    40+ minutes after the tool call itself had already timed out, with no error ever reaching
    the MCP client):
      1. stdin is explicitly closed (`DEVNULL`). Left unset, a child inherits this server's
         own stdin -- which is the live MCP stdio transport, not a real terminal or /dev/null.
         If the child ever attempts to read from stdin for any reason, that read blocks
         forever, since nothing meant for it will ever arrive on that pipe.
      2. On timeout, the WHOLE process tree is killed (see _kill_process_tree), and a clean
         timed-out CompletedProcess is returned -- instead of letting TimeoutExpired escape
         uncaught, which leaves the real OS process running as an orphan with nothing to
         report the failure back to the caller.
    """
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout or SUBPROCESS_TIMEOUT_SECONDS)
        return subprocess.CompletedProcess(args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc.pid)
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001 - already killed; just reclaim the pipes if possible
            pass
        return subprocess.CompletedProcess(
            args, -1, "", f"timed out after {timeout or SUBPROCESS_TIMEOUT_SECONDS}s and was killed"
        )


def _run_git(args: List[str], cwd: Path, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return _run(["git", *args], cwd=cwd, env=env)


def _with_token_auth(repo_url: str) -> str:
    """Same convention as workflow/git_workflow.py: embed the (non-secret) x-access-token
    placeholder username into an https:// URL; the real token travels via GIT_ASKPASS only,
    never in the URL, so it never lands in `.git/config` or shell history."""
    if not GITHUB_TOKEN or not repo_url.startswith("https://"):
        return repo_url
    if "@" in repo_url.split("://", 1)[1]:
        return repo_url
    return repo_url.replace("https://", "https://x-access-token@", 1)


@contextmanager
def _git_auth_env():
    if not GITHUB_TOKEN:
        yield os.environ.copy()
        return
    fd, askpass_path = tempfile.mkstemp(suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write('#!/bin/sh\necho "$GIT_PUSH_TOKEN"\n')
        os.chmod(askpass_path, 0o700)
        env = os.environ.copy()
        env["GIT_ASKPASS"] = askpass_path
        env["GIT_PUSH_TOKEN"] = GITHUB_TOKEN
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"
        yield env
    finally:
        if os.path.exists(askpass_path):
            os.remove(askpass_path)


def _tool_result(tool: str, proc: subprocess.CompletedProcess) -> dict:
    return {
        "tool": tool,
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _environment_gap_hint(result: dict) -> dict:
    """pytest here runs inside THIS server's own venv (provisioned from this server's own
    pyproject.toml), which has none of the target repo's actual third-party dependencies --
    confirmed in practice: a genuinely correct fix in a module that imports e.g.
    snowflake-connector-python fails collection with ModuleNotFoundError, reported as a plain
    FAIL indistinguishable from a real logic bug. Flag that distinction rather than silently
    letting a tooling gap look like a code defect -- does not fix the gap, only labels it."""
    if result["passed"] or "ModuleNotFoundError" not in (result.get("stdout", "") + result.get("stderr", "")):
        return result
    return {
        **result,
        "possible_environment_gap": (
            "This failure is a ModuleNotFoundError, not a test assertion failure -- it may mean "
            "the target repo's own dependency isn't installed in this server's sandboxed venv "
            "(which only has this server's own dependencies), not that the fix is wrong. Verify "
            "by running the same test in an environment with the target repo's actual "
            "requirements installed before treating this as a real REMEDIATION_FAILED."
        ),
    }


def _databricks_client():
    """Lazy Databricks client -- only constructed when get_repo_mapping is actually called, so a
    missing DATABRICKS_HOST/TOKEN never affects the git/lint tools above, which need neither."""
    if not DATABRICKS_HOST or not DATABRICKS_TOKEN:
        raise RuntimeError(
            "DATABRICKS_HOST and DATABRICKS_TOKEN must both be set for get_repo_mapping "
            "(no other tool in this server needs them)."
        )
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(host=DATABRICKS_HOST, token=DATABRICKS_TOKEN)


# Matches a git-clonable URL, optionally with an embedded credential (https://TOKEN@host/... or
# a bare user@host: SSH form), ending in a repo path. Deliberately conservative -- would rather
# miss an unusual URL shape than mis-extract something that isn't really a repo URL.
_GIT_URL_PATTERN = re.compile(
    r"(?:https?://(?:[^@\s\"'/]+@)?[^\s\"']+?\.git|git@[^\s\"':]+:[^\s\"']+?\.git)"
)


def _find_git_url_in_source(source_content: str) -> Optional[str]:
    """Heuristic fallback (Mechanism 3) for jobs whose task code clones a repo manually in
    Python/shell rather than using either of Databricks' official git-linkage mechanisms --
    confirmed in practice, twice, that this is a real, common pattern, not a hypothetical one.
    Returns the FIRST match with any embedded credential stripped, or None if nothing matches.
    Multiple distinct git operations in one file would only ever return the first -- a caller
    that cares should inspect source_content itself rather than assume this is exhaustive."""
    match = _GIT_URL_PATTERN.search(source_content)
    if not match:
        return None
    url = match.group(0)
    if url.startswith("git@"):
        return url  # SSH form has no embeddable token to strip
    # Strip an embedded https://TOKEN@host/... credential -- the caller authenticates via
    # GITHUB_TOKEN/GIT_ASKPASS instead, never via a token baked into the URL itself.
    return re.sub(r"://[^@\s\"'/]+@", "://", url)


# ---------------------------------------------------------------------------
# 1. git_clone
# ---------------------------------------------------------------------------


@mcp.tool()
def git_clone(repo_url: str, target_dir: str) -> dict:
    """Clone repo_url into target_dir (must resolve under the server's workdir). Returns
    repo_dir on success -- pass that to every other git_* tool for this checkout."""
    try:
        dest = _resolve_under_workdir(target_dir)
    except ValueError as exc:
        return {"repo_dir": None, "error": str(exc)}

    if dest.exists() and any(dest.iterdir()):
        return {"repo_dir": None, "error": f"{dest} already exists and is not empty"}
    dest.parent.mkdir(parents=True, exist_ok=True)

    auth_url = _with_token_auth(repo_url)
    with _git_auth_env() as env:
        proc = _run_git(
            ["-c", "credential.helper=", "clone", auth_url, str(dest)],
            cwd=WORKDIR,
            env=env,
        )
    if proc.returncode != 0:
        return {"repo_dir": None, "error": proc.stderr.strip()}
    return {"repo_dir": str(dest), "error": None}


# ---------------------------------------------------------------------------
# 2. git_create_branch
# ---------------------------------------------------------------------------


@mcp.tool()
def git_create_branch(repo_dir: str, branch: str, base: str = "main") -> dict:
    """Check out `base`, pull it fresh, then create and check out `branch` from it."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"branch": None, "error": str(exc)}

    proc = _run_git(["checkout", base], cwd=cwd)
    if proc.returncode != 0:
        return {"branch": None, "error": f"checkout {base} failed: {proc.stderr.strip()}"}

    with _git_auth_env() as env:
        proc = _run_git(["-c", "credential.helper=", "pull", "origin", base], cwd=cwd, env=env)
    if proc.returncode != 0:
        return {"branch": None, "error": f"pull origin {base} failed: {proc.stderr.strip()}"}

    proc = _run_git(["checkout", "-b", branch], cwd=cwd)
    if proc.returncode != 0:
        return {"branch": None, "error": f"checkout -b {branch} failed: {proc.stderr.strip()}"}
    return {"branch": branch, "error": None}


# ---------------------------------------------------------------------------
# 3. git_status
# ---------------------------------------------------------------------------


@mcp.tool()
def git_status(repo_dir: str) -> dict:
    """Current branch + porcelain status -- check this before git_commit to confirm exactly
    what will be staged."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"branch": None, "changed_files": [], "error": str(exc)}

    branch_proc = _run_git(["branch", "--show-current"], cwd=cwd)
    status_proc = _run_git(["status", "--porcelain"], cwd=cwd)
    if status_proc.returncode != 0:
        return {"branch": None, "changed_files": [], "error": status_proc.stderr.strip()}
    changed = [line.strip() for line in status_proc.stdout.splitlines() if line.strip()]
    return {"branch": branch_proc.stdout.strip(), "changed_files": changed, "error": None}


# ---------------------------------------------------------------------------
# 4. git_commit
# ---------------------------------------------------------------------------


@mcp.tool()
def git_commit(repo_dir: str, message: str, files: List[str]) -> dict:
    """`git add <files> && git commit -m <message>`. `files` are paths relative to repo_dir."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"sha": None, "error": str(exc)}
    if not files:
        return {"sha": None, "error": "files must be a non-empty list"}

    proc = _run_git(["add", *files], cwd=cwd)
    if proc.returncode != 0:
        return {"sha": None, "error": f"add failed: {proc.stderr.strip()}"}

    proc = _run_git(["commit", "-m", message], cwd=cwd)
    if proc.returncode != 0:
        return {"sha": None, "error": f"commit failed: {proc.stderr.strip()}"}

    sha_proc = _run_git(["rev-parse", "HEAD"], cwd=cwd)
    return {"sha": sha_proc.stdout.strip(), "error": None}


# ---------------------------------------------------------------------------
# 5. git_push
# ---------------------------------------------------------------------------


@mcp.tool()
def git_push(repo_dir: str, branch: str, remote: str = "origin") -> dict:
    """`git push -u <remote> <branch>`, authenticated via GITHUB_TOKEN if it's set."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"pushed": False, "error": str(exc)}

    with _git_auth_env() as env:
        proc = _run_git(
            ["-c", "credential.helper=", "push", "-u", remote, branch], cwd=cwd, env=env
        )
    if proc.returncode != 0:
        return {"pushed": False, "error": proc.stderr.strip()}
    return {"pushed": True, "error": None}


# ---------------------------------------------------------------------------
# 6. run_static_checks
# ---------------------------------------------------------------------------


@mcp.tool()
def run_static_checks(repo_dir: str, files: List[str]) -> dict:
    """Mirrors the `testing` skill's static-validation step: black --check, isort --check,
    flake8 --max-line-length=120, python -m py_compile on `files` (paths relative to repo_dir,
    Python files only -- anything else in the list is skipped and reported as such). Then, for
    each file under python/ with a matching python/tests/test_<module>.py in the repo, runs that
    test file too. Returns a per-tool breakdown plus one overall PASS/FAIL verdict."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"checked": [], "results": [], "passed": False, "error": str(exc)}

    py_files = [f for f in files if f.endswith(".py")]
    skipped = [f for f in files if not f.endswith(".py")]
    if not py_files:
        return {
            "checked": [],
            "skipped": skipped,
            "results": [],
            "passed": True,
            "error": "no .py files to check",
        }

    results = []
    for label, tool_args in (
        ("black", ["black", "--check", *py_files]),
        ("isort", ["isort", "--check", *py_files]),
        ("flake8", ["flake8", "--max-line-length=120", *py_files]),
        ("py_compile", [sys.executable, "-m", "py_compile", *py_files]),
    ):
        try:
            proc = _run(tool_args, cwd=cwd)
        except FileNotFoundError as exc:
            results.append({"tool": label, "passed": False, "returncode": None,
                             "stdout": "", "stderr": f"not installed/found: {exc}"})
            continue
        results.append(_tool_result(label, proc))

    for f in py_files:
        stem = Path(f).stem
        candidate = cwd / "python" / "tests" / f"test_{stem}.py"
        if candidate.exists():
            try:
                proc = _run(
                    ["pytest", str(candidate.relative_to(cwd)), "-m", "not integration", "-v"],
                    cwd=cwd,
                )
                results.append(_environment_gap_hint(_tool_result(f"pytest:{candidate.name}", proc)))
            except FileNotFoundError as exc:
                results.append({"tool": f"pytest:{candidate.name}", "passed": False,
                                 "returncode": None, "stdout": "", "stderr": str(exc)})

    return {
        "checked": py_files,
        "skipped": skipped,
        "results": results,
        "passed": all(r["passed"] for r in results),
        "error": None,
    }


# ---------------------------------------------------------------------------
# 7. run_pytest
# ---------------------------------------------------------------------------


@mcp.tool()
def run_pytest(repo_dir: str, test_path: str, markers: str = "not integration") -> dict:
    """Run pytest against a specific test path (file or directory, relative to repo_dir) with
    -m <markers>. Use this for ad-hoc/retry test runs outside run_static_checks' fixed
    test_<module>.py convention."""
    try:
        cwd = _resolve_under_workdir(repo_dir, must_exist=True)
    except ValueError as exc:
        return {"passed": False, "stdout": "", "stderr": str(exc), "returncode": None}

    try:
        proc = _run(["pytest", test_path, "-m", markers, "-v"], cwd=cwd)
    except FileNotFoundError as exc:
        return {"passed": False, "stdout": "", "stderr": str(exc), "returncode": None}
    return _environment_gap_hint({
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    })


# ---------------------------------------------------------------------------
# 8. get_repo_mapping
# ---------------------------------------------------------------------------


@mcp.tool()
def get_repo_mapping(source_path: str, job_id: str = "", source_content: str = "") -> dict:
    """
    Resolve a Databricks task's source_path to the git repo it actually lives in, trying three
    mechanisms in order:

      1. Databricks Repos -- source_path is a workspace path under /Repos/...; the path itself
         identifies the Repo (and therefore the git remote/branch) it was checked out from.
      2. Job-level Git source -- the task's "Source" is set to a Git provider directly (Jobs UI:
         Job details -> Git). Here source_path is *relative to the repo root* (no leading "/"),
         and the repo URL/branch live on the job's settings.git_source instead -- pass job_id for
         this case to resolve at all.
      3. Heuristic source scan -- if 1 and 2 both find nothing, and you pass the task's actual
         source (source_content, e.g. from a fetch via another server's get_source_file), scans
         it for a hardcoded git URL. This covers jobs whose task code clones a repo manually in
         Python/shell rather than using either official mechanism -- confirmed in practice, twice,
         building this plugin, that this is common, not a hypothetical. Any embedded credential
         in that URL is stripped before it's returned.

    A source_path resolved by none of the three returns repo_url=None -- treat that as "there is
    no git repo to fix this in", not something to retry.
    """
    empty = {
        "source_path": source_path,
        "repo_url": None,
        "repo_path_in_workspace": None,
        "relative_path_in_repo": None,
        "branch": None,
        "provider": None,
        "resolution_method": None,
    }

    if not source_path:
        return {**empty, "error": "empty source_path"}

    from databricks.sdk.errors import DatabricksError

    # --- Mechanism 1: Databricks Repos (workspace path under /Repos/...) ---
    if source_path.startswith("/Repos/"):
        try:
            client = _databricks_client()
        except RuntimeError as exc:
            return {**empty, "error": str(exc)}

        # Walk up from the full path to find the repo root, trying repos.get() at each
        # level -- NOT client.repos.list() + prefix match. Confirmed in practice: repos.list()
        # can lag well behind repos.create() (a repo that resolved instantly via
        # workspace.get_status() + repos.get() still hadn't appeared in repos.list() after
        # 5+ minutes), which would make a just-created/just-repointed Repo invisible to this
        # tool for an unpredictable stretch. workspace.get_status() + repos.get() reflects a
        # repo immediately, with no such lag observed.
        found_repo = None
        found_repo_path = None
        parts = source_path.strip("/").split("/")
        for i in range(len(parts), 1, -1):
            candidate_path = "/" + "/".join(parts[:i])
            try:
                status = client.workspace.get_status(candidate_path)
            except DatabricksError:
                continue
            try:
                found_repo = client.repos.get(repo_id=status.object_id)
                found_repo_path = candidate_path
                break
            except DatabricksError:
                continue  # this directory level isn't a repo root -- keep walking up

        if found_repo:
            repo_path = found_repo_path.rstrip("/")
            return {
                "source_path": source_path,
                "repo_url": getattr(found_repo, "url", None),
                "repo_path_in_workspace": repo_path,
                "relative_path_in_repo": source_path[len(repo_path):].lstrip("/"),
                "branch": getattr(found_repo, "branch", None),
                "provider": (str(getattr(found_repo, "provider", "")) or None),
                "resolution_method": "databricks_repos",
                "error": None,
            }
        # Falls through to Mechanism 3 below rather than erroring immediately -- a /Repos/ path
        # Databricks doesn't recognize is unusual but not proof there's no heuristic answer.

    # --- Mechanism 2: job-level Git source ---
    elif not source_path.startswith("/"):
        if not job_id:
            return {
                **empty,
                "error": (
                    "source_path looks like a repo-relative path (no leading '/', not under "
                    "/Repos/), which means this task's Source is set to a Git provider rather "
                    "than a workspace path. Pass job_id so the job's git_source can be resolved, "
                    "or pass source_content for the heuristic fallback instead."
                ),
            }
        try:
            client = _databricks_client()
        except RuntimeError as exc:
            return {**empty, "error": str(exc)}
        try:
            job = client.jobs.get(job_id=int(job_id))
        except DatabricksError as exc:
            return {**empty, "error": f"could not fetch job {job_id}: {exc}"}

        git_source = getattr(job.settings, "git_source", None) if job.settings else None
        if git_source and getattr(git_source, "git_url", None):
            return {
                "source_path": source_path,
                "repo_url": git_source.git_url,
                "repo_path_in_workspace": None,
                "relative_path_in_repo": source_path,
                "branch": (
                    getattr(git_source, "git_branch", None)
                    or getattr(git_source, "git_tag", None)
                    or getattr(git_source, "git_commit", None)
                ),
                "provider": str(getattr(git_source, "git_provider", "")) or None,
                "resolution_method": "job_git_source",
                "error": None,
            }
        # No git_source configured -- falls through to Mechanism 3 below.

    # --- Mechanism 3: heuristic scan of already-fetched task source ---
    if source_content:
        found_url = _find_git_url_in_source(source_content)
        if found_url:
            return {
                "source_path": source_path,
                "repo_url": found_url,
                "repo_path_in_workspace": None,
                "relative_path_in_repo": None,
                "branch": None,
                "provider": None,
                "resolution_method": "heuristic_source_scan",
                "error": (
                    "Resolved via a heuristic scan of the task's own source for a hardcoded git "
                    "URL, NOT via Databricks' tracked git-linkage -- treat as a strong signal, "
                    "not a guarantee. Note this explicitly in any report."
                ),
            }

    return {
        **empty,
        "error": (
            "No git repo found for this source_path via Databricks Repos, job-level Git source, "
            "or (if source_content was passed) a hardcoded URL in the task's own code. This task "
            "may not be linked to any git repo at all."
        ),
    }


# ---------------------------------------------------------------------------
# 9. create_pr / 10. find_open_pr
# ---------------------------------------------------------------------------


def _ensure_ca_bundle() -> None:
    """If EXTRA_CA_CERT is configured, build a combined cert bundle (the normal public CA bundle
    plus that extra corporate-proxy root CA) once and point REQUESTS_CA_BUNDLE at it, so
    PyGithub's underlying `requests` calls trust both. Without this, every create_pr/find_open_pr
    call fails outright behind a TLS-intercepting corporate proxy -- confirmed in practice, not
    a hypothetical. A no-op if EXTRA_CA_CERT isn't set, the file doesn't exist, or the caller
    already set REQUESTS_CA_BUNDLE explicitly (never override an explicit choice)."""
    if not EXTRA_CA_CERT or not os.path.exists(EXTRA_CA_CERT):
        return
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    combined_path = WORKDIR / ".combined-ca-bundle.pem"
    if not combined_path.exists():
        import certifi

        with open(certifi.where(), "r", encoding="utf-8") as f:
            base_bundle = f.read()
        with open(EXTRA_CA_CERT, "r", encoding="utf-8") as f:
            extra_cert = f.read()
        combined_path.write_text(base_bundle + "\n" + extra_cert, encoding="utf-8")
    os.environ["REQUESTS_CA_BUNDLE"] = str(combined_path)


def _github_client():
    """Lazy PyGithub client -- only constructed when create_pr/find_open_pr are actually
    called, so a missing GITHUB_TOKEN never affects any git_* tool, none of which need it for
    public repos or SSH remotes."""
    if not GITHUB_TOKEN:
        raise RuntimeError(
            "GITHUB_TOKEN must be set for create_pr/find_open_pr (the GitHub API always needs "
            "a token, unlike git_clone/git_push which can work without one for public repos "
            "or SSH remotes)."
        )
    _ensure_ca_bundle()
    from github import Auth, Github

    return Github(auth=Auth.Token(GITHUB_TOKEN))


@mcp.tool()
def create_pr(repo: str, branch: str, base: str, title: str, body: str) -> dict:
    """Create a GitHub pull request via the GitHub API (head=branch, base=base, on `repo` as
    "owner/name"). Returns pr_number/pr_url on success. Does not touch Jira at all -- handle
    ticket transitions/comments separately (e.g. via the Atlassian connector). Never merges or
    closes anything -- this plugin's entire design never merges its own PR."""
    try:
        gh = _github_client()
    except RuntimeError as exc:
        return {"pr_number": None, "pr_url": None, "error": str(exc)}
    try:
        gh_repo = gh.get_repo(repo)
        pr = gh_repo.create_pull(title=title, body=body, head=branch, base=base)
        return {"pr_number": pr.number, "pr_url": pr.html_url, "error": None}
    except Exception as exc:  # noqa: BLE001 - PyGithub raises its own exception hierarchy; a
        # clean {"error": ...} beats a caller having to catch a library-specific exception type
        return {"pr_number": None, "pr_url": None, "error": str(exc)}


@mcp.tool()
def find_open_pr(repo: str, search_text: str) -> dict:
    """Search open PRs on `repo` (owner/name) for one whose title or branch name contains
    search_text (e.g. a run ID or ticket key) -- for opsbuddy-fix Phase 4's dedup check, so it
    reuses an existing PR for this incident instead of opening a duplicate."""
    empty = {"found": False, "pr_number": None, "pr_url": None, "branch": None}
    try:
        gh = _github_client()
    except RuntimeError as exc:
        return {**empty, "error": str(exc)}
    try:
        gh_repo = gh.get_repo(repo)
        for pr in gh_repo.get_pulls(state="open"):
            if search_text in (pr.title or "") or search_text in (pr.head.ref or ""):
                return {
                    "found": True,
                    "pr_number": pr.number,
                    "pr_url": pr.html_url,
                    "branch": pr.head.ref,
                    "error": None,
                }
        return {**empty, "error": None}
    except Exception as exc:  # noqa: BLE001 - see create_pr
        return {**empty, "error": str(exc)}


# ---------------------------------------------------------------------------
# 11. post_slack_alert
# ---------------------------------------------------------------------------


# Five checkpoints across the pipeline, each with its own header/emoji so a channel reads as a
# clear timeline rather than one undifferentiated "update" repeated five times.
_STAGE_HEADERS = {
    "incident_detected": "\U0001f6a8 opsbuddy-fix -- incident detected",
    "pr_opened": "\U0001f500 opsbuddy-fix -- PR opened (not yet merged)",
    "pr_merged": "✅ opsbuddy-fix -- PR merged",
    "verification_running": "⏳ opsbuddy-fix -- verifying fix (re-run in progress)",
    "resolved": "\U0001f389 opsbuddy-fix -- incident resolved",
}


def _incident_summary_blocks(incident: dict, stage: str = "", message: str = "") -> list:
    header_text = _STAGE_HEADERS.get(stage, "opsbuddy-fix incident summary")
    fields = [
        {"type": "mrkdwn", "text": f"*{key}*\n{value or '-'}"} for key, value in incident.items()
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "fields": fields},
    ]
    if message:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": message}})
    return blocks


@mcp.tool()
def post_slack_alert(
    jira_ticket_id: str = "",
    job_id: str = "",
    databricks_run_id: str = "",
    error_category: str = "",
    pr_url: str = "",
    pr_review_verdict: str = "",
    execution_status: str = "",
    stage: str = "",
    message: str = "",
    thread_ts: str = "",
) -> dict:
    """Send one opsbuddy-fix Slack checkpoint. Call this up to five times per run, once per
    checkpoint:
      stage="incident_detected"      -- right after Phase 3 files the Jira ticket. Put the plain-
                                         English root cause / RCA summary in `message`.
      stage="pr_opened"               -- Phase 7, PR opened but not yet merged. Put pr_url.
      stage="pr_merged"                -- after the human approves the Merge Approval Gate.
      stage="verification_running"    -- Gate 8.5, right before triggering the real re-run.
      stage="resolved"                 -- Phase 10, final outcome (Phase 9's ticket-update fields).
    `stage` only changes the header/emoji shown in Slack -- every other field behaves exactly as
    before, and `stage=""` still sends the original generic "incident summary" header, so existing
    callers that don't pass it keep working unchanged. `message` is free text (e.g. the RCA
    paragraph or a one-line verification result) shown as its own block below the field grid.
    `job_id` is the Databricks job being fixed -- distinct from `databricks_run_id`, which is a
    specific *run* of that job; pass both when known (job_id stays the same across all five
    checkpoints of one incident, run_id may not).

    **Threading**: pass no `thread_ts` on the first call (stage="incident_detected") -- that posts
    the thread's parent. If the response includes a `ts`, keep it and pass it back as `thread_ts`
    on every later checkpoint's call for this same incident, so all five land as replies in one
    thread instead of five separate top-level messages. Threading needs SLACK_BOT_TOKEN +
    SLACK_CHANNEL_ID configured (the Slack Web API's chat.postMessage) -- used automatically
    instead of the webhook whenever both are set. Falls back to SLACK_WEBHOOK_URL if the bot token
    isn't configured; `thread_ts` is silently ignored on that path (a webhook can't thread at all,
    and always posts a new top-level message; the response's `ts` comes back `None`).

    Returns {"sent": bool, "ts": "170000...123", "channel": "C0123...", "error": None} on the
    bot-token path (ts/channel needed for a later reply), {"sent": bool, "ts": None,
    "channel": None, "error": None} on the webhook path (nothing to thread into later), or
    {"sent": False, "ts": None, "channel": None, "error": "..."} if neither is configured or the
    call fails.

    Mirrors workflow/slack_workflow.py's send-incident-summary exactly (same fields, same block
    layout, same threading contract) so the message looks identical regardless of which client
    sent it."""
    incident = {
        "Jira Ticket": jira_ticket_id,
        "Job ID": job_id,
        "Databricks Run ID": databricks_run_id,
        "Error Category": error_category,
        "PR": pr_url,
        "Review Verdict": pr_review_verdict,
        "Execution Status": execution_status,
    }
    text = f"[opsbuddy-fix] {jira_ticket_id or databricks_run_id} -- {stage or execution_status or 'update'}"
    blocks = _incident_summary_blocks(incident, stage=stage, message=message)
    import requests

    # Same CA-bundle fix create_pr/find_open_pr and the webhook path below already needed, for
    # the same reason: behind a TLS-intercepting corporate proxy, requests.post() fails outright
    # with SSLCertVerificationError against the public CA bundle alone. Confirmed in practice --
    # this was missing on this specific path (only the webhook fallback had it) and broke the
    # very first live bot-token Slack call.
    _ensure_ca_bundle()

    if SLACK_BOT_TOKEN and SLACK_CHANNEL_ID:
        payload = {"channel": SLACK_CHANNEL_ID, "text": text, "blocks": blocks}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=10,
            )
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - network/DNS/timeout, all reduce to one verdict
            return {"sent": False, "ts": None, "channel": None, "error": str(exc)}
        if not data.get("ok"):
            return {"sent": False, "ts": None, "channel": None, "error": f"Slack API error: {data.get('error')}"}
        return {"sent": True, "ts": data.get("ts"), "channel": data.get("channel"), "error": None}

    if not SLACK_WEBHOOK_URL:
        return {
            "sent": False,
            "ts": None,
            "channel": None,
            "error": "Neither SLACK_BOT_TOKEN+SLACK_CHANNEL_ID nor SLACK_WEBHOOK_URL is configured.",
        }
    try:
        response = requests.post(
            SLACK_WEBHOOK_URL,
            json={"text": text, "blocks": blocks},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - network/DNS/timeout, all reduce to one verdict
        return {"sent": False, "ts": None, "channel": None, "error": str(exc)}
    if response.status_code != 200:
        return {
            "sent": False,
            "ts": None,
            "channel": None,
            "error": f"Slack webhook returned {response.status_code}: {response.text}",
        }
    return {"sent": True, "ts": None, "channel": None, "error": None}


# ---------------------------------------------------------------------------
# 12. log_incident
# ---------------------------------------------------------------------------


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


@mcp.tool()
def log_incident(record: dict) -> dict:
    """Write one row into the Databricks ops incident-log table (DATABRICKS_OPS_INCIDENT_TABLE,
    default dev.ops_incidents.incident_log). This table predates this plugin (built for an
    earlier email-alert design, before the pivot to Slack) so its columns don't match this
    skill's own vocabulary one-for-one -- don't rely on the opsbuddy-fix skill being loaded to
    know the shape; confirmed in practice that it can be missing from context when this tool is
    called. `record` must have exactly these keys (verified via DESCRIBE TABLE and a real
    insert/read-back/delete cycle):

        incident_id (str), jira_ticket_id (str), databricks_job_id (int), databricks_run_id (int),
        job_name (str), task_key (str), error_category (str), root_cause_summary (str),
        stack_trace_excerpt (str), code_fix_possible (bool), target_repo (str), branch_name (str),
        commit_sha (str), pr_url (str), pr_review_verdict (str), execution_status (str),
        severity (str), detected_at (ISO timestamp str -- no default, insert fails without it),
        resolved_at (ISO timestamp str, omit this key entirely if not yet resolved),
        email_sent (bool -- this table has no Slack-specific column; reuse this one to mean "an
        alert was sent" regardless of channel), email_recipients (str -- the Slack channel or
        webhook target actually used, or "").

    Use "" for any string field with no value, not null/None, except detected_at (required) and
    resolved_at (omit the key). A missing/misnamed column fails with UNRESOLVED_COLUMN or
    DELTA_INSERT_COLUMN_MISMATCH naming the real column -- if a future table redesign changes
    these names, update this docstring to match rather than guessing from the error alone each
    time. Mirrors python/utils/databricks_conn.py's insert_ops_incident_log exactly (same SQL
    construction, same `loaded_at` default) so behavior stays identical to the Bash path."""
    if not DATABRICKS_SQL_WAREHOUSE_ID:
        return {
            "logged": False,
            "error": (
                "DATABRICKS_SQL_WAREHOUSE_ID must be set for log_incident (no other tool in "
                "this server needs it) -- reuse the same value already configured for the "
                "databricks-lineage plugin's DATABRICKS_SQL_WAREHOUSE_ID, if one is registered."
            ),
        }
    try:
        client = _databricks_client()
    except RuntimeError as exc:
        return {"logged": False, "error": str(exc)}

    columns = list(record.keys()) + ["loaded_at"]
    values = [_sql_literal(v) for v in record.values()] + ["current_timestamp()"]
    sql = (
        f"INSERT INTO {DATABRICKS_OPS_INCIDENT_TABLE} ({', '.join(columns)}) "
        f"VALUES ({', '.join(values)})"
    )

    from databricks.sdk.errors import DatabricksError

    try:
        response = client.statement_execution.execute_statement(
            statement=sql, warehouse_id=DATABRICKS_SQL_WAREHOUSE_ID, wait_timeout="30s"
        )
    except DatabricksError as exc:
        return {"logged": False, "error": str(exc)}

    status = response.status
    if status and status.state and status.state.value != "SUCCEEDED":
        return {"logged": False, "error": f"Databricks SQL statement failed: {status}"}
    return {"logged": True, "incident_id": record.get("incident_id"), "error": None}


# ---------------------------------------------------------------------------
# 13. read_file / 14. write_file
# ---------------------------------------------------------------------------


@mcp.tool()
def read_file(repo_dir: str, path: str) -> dict:
    """Read a text file at `path` (relative to an already-cloned `repo_dir`). For Phase 5 on
    Claude Desktop, which has no file-reading tool of its own -- read the file here, edit its
    content, then pass the whole new content to write_file. Whole-file only; there is no
    line-range/patch mode. Text files only -- a binary file will fail to decode as UTF-8."""
    try:
        target = _resolve_repo_relative(repo_dir, path)
    except ValueError as exc:
        return {"content": None, "error": str(exc)}
    if not target.exists():
        return {"content": None, "error": f"{target} does not exist"}
    if not target.is_file():
        return {"content": None, "error": f"{target} is not a file"}
    try:
        content = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        return {"content": None, "error": f"not a UTF-8 text file: {exc}"}
    return {"content": content, "error": None}


@mcp.tool()
def write_file(repo_dir: str, path: str, content: str) -> dict:
    """Write `content` as the complete new contents of the file at `path` (relative to an
    already-cloned `repo_dir`), overwriting it if it exists or creating it (and any missing
    parent directories) if not. Whole-file only -- always read_file first and edit its content in
    full, rather than guessing at a partial patch. This is a plain file write, not a git
    operation -- git_status/git_commit still need to be called afterward to stage and commit it."""
    try:
        target = _resolve_repo_relative(repo_dir, path)
    except ValueError as exc:
        return {"written": False, "error": str(exc)}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return {"written": False, "error": str(exc)}
    return {"written": True, "path": str(target), "error": None}


# ---------------------------------------------------------------------------
# 15. get_job_run
# ---------------------------------------------------------------------------


_FAILED_RESULT_STATES = ("FAILED", "TIMEDOUT", "CANCELED")
_TERMINAL_LIFE_CYCLE_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")


def _pick_failed_task(run):
    tasks = run.tasks or []
    for task in tasks:
        state = task.state
        if state and state.result_state and state.result_state.value in _FAILED_RESULT_STATES:
            return task
    return tasks[0] if tasks else None


def _extract_run_parameters(run) -> dict:
    params: dict = {}
    for task in run.tasks or []:
        notebook_task = getattr(task, "notebook_task", None)
        if notebook_task and notebook_task.base_parameters:
            params.update(notebook_task.base_parameters)
    return params


@mcp.tool()
def get_job_run(run_id: str) -> dict:
    """Fetch full failure telemetry for a Databricks job run: job/task identifiers, life-cycle
    and result state, error message, stack trace, cluster ID, run parameters, run page URL.
    Needs DATABRICKS_HOST/DATABRICKS_TOKEN (same as get_repo_mapping). Mirrors
    workflow/databricks_workflow.py's DatabricksClient.get_run_failure exactly, so opsbuddy-fix's
    Phase 1 has a real MCP-preferred path bundled with this plugin instead of depending on the
    separate databricks-job-lineage plugin being installed."""
    try:
        client = _databricks_client()
    except RuntimeError as exc:
        return {"error": str(exc)}

    from databricks.sdk.errors import DatabricksError

    try:
        run = client.jobs.get_run(run_id=int(run_id))
    except DatabricksError as exc:
        return {"error": str(exc)}
    except (TypeError, ValueError):
        return {"error": f"run_id must be an integer, got {run_id!r}"}

    task = _pick_failed_task(run)
    error_message, stack_trace = "", ""
    try:
        output = client.jobs.get_run_output(run_id=(task.run_id if task else run.run_id))
        error_message = output.error or ""
        stack_trace = output.error_trace or ""
    except Exception:  # noqa: BLE001 - SDK/network edge cases -- degrade gracefully, same as CLI
        pass

    state = run.state
    return {
        "job_id": run.job_id,
        "run_id": run.run_id,
        "job_name": run.run_name or "",
        "task_key": task.task_key if task else "",
        "life_cycle_state": (
            state.life_cycle_state.value if state and state.life_cycle_state else "UNKNOWN"
        ),
        "result_state": state.result_state.value if state and state.result_state else "-",
        "error_message": error_message,
        "stack_trace": stack_trace,
        "cluster_id": getattr(task, "existing_cluster_id", None) if task else None,
        "run_page_url": run.run_page_url or "",
        "parameters": _extract_run_parameters(run),
        "error": None,
    }


# ---------------------------------------------------------------------------
# 16. get_latest_failed_run
# ---------------------------------------------------------------------------


@mcp.tool()
def get_latest_failed_run(job_id: str) -> dict:
    """Resolve the most recent failed run ID for a job (looks back up to 25 runs). Use this
    when only a job ID is known, not a specific run ID. Mirrors
    workflow/databricks_workflow.py's get_latest_failed_run exactly."""
    try:
        client = _databricks_client()
    except RuntimeError as exc:
        return {"run_id": None, "error": str(exc)}

    from databricks.sdk.errors import DatabricksError

    try:
        for run in client.jobs.list_runs(job_id=int(job_id), active_only=False, limit=25):
            state = run.state
            result_state = state.result_state.value if state and state.result_state else None
            if result_state in _FAILED_RESULT_STATES:
                return {"run_id": run.run_id, "error": None}
    except DatabricksError as exc:
        return {"run_id": None, "error": str(exc)}
    except (TypeError, ValueError):
        return {"run_id": None, "error": f"job_id must be an integer, got {job_id!r}"}
    return {"run_id": None, "error": f"No failed runs found for job {job_id}"}


# ---------------------------------------------------------------------------
# 17. trigger_job_run
# ---------------------------------------------------------------------------


@mcp.tool()
def trigger_job_run(job_id: str, timeout_seconds: int = 600, force: bool = False) -> dict:
    """Re-run a persistent Databricks job and block until it reaches a terminal state -- used
    for opsbuddy-fix's Gate 8.5 real-verification step, to prove a fix actually works rather
    than trusting a code review alone. Real production jobs can write real data, so unless
    job_id is listed in OPSBUDDY_VERIFY_ALLOWLIST (comma-separated job IDs, or "all"), this
    refuses to run unless force=True -- which should only be passed after a human has
    explicitly approved this specific re-run, never auto-approved. This call blocks for up to
    timeout_seconds (default 600s / 10 minutes) -- same blocking behavior as the CLI it mirrors,
    workflow/databricks_workflow.py's DatabricksClient.trigger_and_wait."""
    allowlist = os.environ.get("OPSBUDDY_VERIFY_ALLOWLIST", "").strip()
    allowed = allowlist.lower() == "all" or str(job_id) in {
        x.strip() for x in allowlist.split(",") if x.strip()
    }
    if not force and not allowed:
        return {
            "succeeded": None,
            "error": (
                f"job_id {job_id} is not in OPSBUDDY_VERIFY_ALLOWLIST. Re-running a real job "
                "needs explicit human approval first -- this is not something to auto-approve. "
                "Once a human has approved it, retry with force=True."
            ),
        }
    try:
        client = _databricks_client()
    except RuntimeError as exc:
        return {"succeeded": None, "error": str(exc)}

    from databricks.sdk.errors import DatabricksError

    try:
        run = client.jobs.run_now(job_id=int(job_id))
    except DatabricksError as exc:
        return {"succeeded": None, "error": str(exc)}
    except (TypeError, ValueError):
        return {"succeeded": None, "error": f"job_id must be an integer, got {job_id!r}"}

    run_id = run.run_id
    elapsed = 0
    poll_interval = 10
    while elapsed < timeout_seconds:
        run_status = client.jobs.get_run(run_id=run_id)
        state = run_status.state
        life_cycle = (
            state.life_cycle_state.value if state and state.life_cycle_state else "UNKNOWN"
        )
        result_state = state.result_state.value if state and state.result_state else "-"
        if life_cycle in _TERMINAL_LIFE_CYCLE_STATES:
            return {
                "run_id": run_id,
                "life_cycle_state": life_cycle,
                "result_state": result_state,
                "run_page_url": run_status.run_page_url or "",
                "succeeded": result_state == "SUCCESS",
                "error": None,
            }
        time.sleep(poll_interval)
        elapsed += poll_interval
    return {
        "run_id": run_id,
        "succeeded": None,
        "error": f"Run {run_id} did not finish within {timeout_seconds}s",
    }


# ---------------------------------------------------------------------------
# 18. get_table_lineage
# ---------------------------------------------------------------------------


def _job_name(client, job_id) -> str:
    """Best-effort job name lookup, for annotating a lineage consumer that's a job rather than a
    bare ID -- never worth failing the whole lineage call over."""
    try:
        job = client.jobs.get(job_id=int(job_id))
        return job.settings.name if job.settings and job.settings.name else f"job {job_id}"
    except Exception:  # noqa: BLE001
        return f"job {job_id}"


@mcp.tool()
def get_table_lineage(run_id: str) -> dict:
    """Unity Catalog table lineage for a specific job run -- which tables the run's task(s) read
    from and wrote to, plus anything downstream that reads from those same tables (the actual
    blast radius of a bad run, not just "which tasks in this job failed" like get_job_run's
    downstream-task info already covers). This is real data lineage, distinct from that --
    get_job_run only ever looks inside one job's own task DAG; this looks at the tables
    themselves across the whole workspace.

    Needs DATABRICKS_HOST/DATABRICKS_TOKEN (same as get_repo_mapping/get_job_run) plus
    DATABRICKS_SQL_WAREHOUSE_ID -- reuse the same value already configured for log_incident and
    for the sibling databricks-job-lineage plugin's own DATABRICKS_SQL_WAREHOUSE_ID if one is
    registered, rather than a new credential. Queries system.access.table_lineage via a SQL
    warehouse; requires Unity Catalog lineage tracking enabled on the workspace. The exact column
    names of that system table can vary by workspace/Databricks release -- this assumes
    entity_type/entity_run_id/source_table_full_name/target_table_full_name; if this starts
    erroring, run `DESCRIBE system.access.table_lineage` in a SQL editor and adjust the queries
    below to match.

    Also resolves one hop of lineage in **both** directions, not just downstream: `upstream_producers`
    is whatever wrote the tables this run *read* (the thing to check if the real root cause is
    bad data from further back in the pipeline, not this run's own code), symmetric to
    `downstream_consumers` (whatever reads the tables this run *wrote*). Neither is transitive --
    this is one hop each way, not a full DAG walk; call this again on an upstream producer's own
    run_id if you need to go back further.

    Fails soft, same philosophy as every other tool here: returns an `error` field instead of
    raising when something's wrong (no warehouse configured, UC lineage not enabled, the query
    itself errors), and returns genuinely empty lists when there's honestly nothing there (the
    run never got far enough to read/write anything) -- don't let a caller mistake "couldn't
    check" for "there's nothing there."
    """
    empty = {
        "tables_read": [],
        "tables_written": [],
        "upstream_producers": [],
        "downstream_consumers": [],
    }
    if not DATABRICKS_SQL_WAREHOUSE_ID:
        return {
            **empty,
            "error": (
                "DATABRICKS_SQL_WAREHOUSE_ID is not configured -- table lineage requires a SQL "
                "warehouse to query Unity Catalog system tables."
            ),
        }
    try:
        client = _databricks_client()
    except RuntimeError as exc:
        return {**empty, "error": str(exc)}

    from databricks.sdk.errors import DatabricksError

    def run_query(statement):
        resp = client.statement_execution.execute_statement(
            warehouse_id=DATABRICKS_SQL_WAREHOUSE_ID,
            statement=statement,
            wait_timeout="30s",
        )
        if not resp.result or not resp.result.data_array:
            return []
        return resp.result.data_array

    try:
        rows = run_query(
            f"""
            SELECT DISTINCT source_table_full_name, target_table_full_name
            FROM system.access.table_lineage
            WHERE entity_type = 'JOB' AND entity_run_id = '{run_id}'
            """
        )
    except DatabricksError as exc:
        return {**empty, "error": f"table_lineage query failed: {exc}"}

    tables_read = sorted({r[0] for r in rows if r and r[0]})
    tables_written = sorted({r[1] for r in rows if r and r[1]})

    def neighbor_entities(tables, column):
        """One hop of lineage neighbors touching `tables` via `column`
        (source_table_full_name for consumers of what we wrote, target_table_full_name for
        producers of what we read), excluding this run itself."""
        if not tables:
            return [], None
        in_clause = ", ".join(f"'{t}'" for t in tables)
        try:
            rows = run_query(
                f"""
                SELECT DISTINCT entity_type, entity_id
                FROM system.access.table_lineage
                WHERE {column} IN ({in_clause})
                  AND entity_run_id != '{run_id}'
                """
            )
        except DatabricksError as exc:
            return [], str(exc)
        entities = [
            {
                "type": (entity_type or "unknown").lower(),
                "name": _job_name(client, entity_id) if entity_type == "JOB" else str(entity_id),
                "id": str(entity_id),
            }
            for entity_type, entity_id in rows
        ]
        return entities, None

    upstream_producers, upstream_error = neighbor_entities(tables_read, "target_table_full_name")
    if upstream_error:
        return {
            "tables_read": tables_read,
            "tables_written": tables_written,
            "upstream_producers": [],
            "downstream_consumers": [],
            "error": f"upstream producer lookup failed (tables read/written above are still valid): {upstream_error}",
        }

    downstream_consumers, downstream_error = neighbor_entities(tables_written, "source_table_full_name")
    if downstream_error:
        return {
            "tables_read": tables_read,
            "tables_written": tables_written,
            "upstream_producers": upstream_producers,
            "downstream_consumers": [],
            "error": f"downstream consumer lookup failed (everything else above is still valid): {downstream_error}",
        }

    return {
        "tables_read": tables_read,
        "tables_written": tables_written,
        "upstream_producers": upstream_producers,
        "downstream_consumers": downstream_consumers,
        "error": None,
    }


def _run_http() -> None:
    """Serve over Streamable HTTP behind a bearer-token check -- see the databricks-lineage
    server's README for what this trades off; same shape here."""
    if not MCP_API_KEY:
        print(
            "FATAL: MCP_TRANSPORT=http requires MCP_API_KEY to be set.\n"
            "  export MCP_API_KEY=<a long random string>\n"
            "  (generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\")",
            file=sys.stderr,
        )
        sys.exit(1)

    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class BearerTokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            expected = f"Bearer {MCP_API_KEY}"
            if request.headers.get("authorization") != expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    app = mcp.streamable_http_app()
    app.add_middleware(BearerTokenMiddleware)
    print(
        f"Serving opsbuddy-git-ops over HTTP on {MCP_HOST}:{MCP_PORT}/mcp "
        "(bearer-token auth required)",
        file=sys.stderr,
    )
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    if MCP_TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    elif MCP_TRANSPORT in ("http", "streamable-http"):
        _run_http()
    else:
        print(f"FATAL: unknown MCP_TRANSPORT={MCP_TRANSPORT!r} (expected 'stdio' or 'http')",
              file=sys.stderr)
        sys.exit(1)
