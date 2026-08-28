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
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

from mcp.server.fastmcp import FastMCP

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
    for tool_args in (
        ["black", "--check", *py_files],
        ["isort", "--check", *py_files],
        ["flake8", "--max-line-length=120", *py_files],
        [sys.executable, "-m", "py_compile", *py_files],
    ):
        try:
            proc = _run(tool_args, cwd=cwd)
        except FileNotFoundError as exc:
            results.append({"tool": tool_args[0], "passed": False, "returncode": None,
                             "stdout": "", "stderr": f"not installed/found: {exc}"})
            continue
        results.append(_tool_result(tool_args[0], proc))

    for f in py_files:
        stem = Path(f).stem
        candidate = cwd / "python" / "tests" / f"test_{stem}.py"
        if candidate.exists():
            try:
                proc = _run(
                    ["pytest", str(candidate.relative_to(cwd)), "-m", "not integration", "-v"],
                    cwd=cwd,
                )
                results.append(_tool_result(f"pytest:{candidate.name}", proc))
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
    return {
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


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
        try:
            best_match = None
            for repo in client.repos.list():
                repo_path = getattr(repo, "path", None)
                if repo_path and source_path.startswith(repo_path.rstrip("/") + "/"):
                    if best_match is None or len(repo_path) > len(best_match.path):
                        best_match = repo
        except DatabricksError as exc:
            return {**empty, "error": f"could not list Databricks Repos: {exc}"}

        if best_match:
            repo_path = best_match.path.rstrip("/")
            return {
                "source_path": source_path,
                "repo_url": getattr(best_match, "url", None),
                "repo_path_in_workspace": repo_path,
                "relative_path_in_repo": source_path[len(repo_path):].lstrip("/"),
                "branch": getattr(best_match, "branch", None),
                "provider": (str(getattr(best_match, "provider", "")) or None),
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
