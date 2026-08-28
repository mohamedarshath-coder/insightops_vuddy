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

Every path argument (target_dir, repo_dir) must resolve underneath OPSBUDDY_MCP_WORKDIR (default:
a workdir/ folder next to this file) -- this server refuses to touch anything outside it, so a
bad or malicious argument can't walk it into unrelated parts of the filesystem.

Auth: GITHUB_TOKEN (optional -- only needed for HTTPS clone/push against a private repo; SSH
remotes need nothing from this server). Nothing here talks to the GitHub API directly; that's
still the "github" MCP server's job (create_pull_request, etc.) -- this one only ever runs local
`git` CLI commands, mirroring workflow/git_workflow.py's GitRepoManager from the Claude Code side
of this plugin.

Run it:
    pip install -r requirements.txt
    export GITHUB_TOKEN=ghp_...        # optional, HTTPS clones/pushes of private repos only
    export OPSBUDDY_MCP_WORKDIR=D:\opsbuddy\opsbuddy-git-workdir   # optional, see default below
    python server.py

Then point an MCP client (Claude Desktop, Claude Code, etc.) at it as a stdio server -- see
README.md for the exact client config.
"""

import os
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


def _run_git(args: List[str], cwd: Path, env: Optional[dict] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
    )


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
            proc = subprocess.run(
                tool_args, cwd=str(cwd), capture_output=True, text=True,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
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
                proc = subprocess.run(
                    ["pytest", str(candidate.relative_to(cwd)), "-m", "not integration", "-v"],
                    cwd=str(cwd), capture_output=True, text=True,
                    timeout=SUBPROCESS_TIMEOUT_SECONDS,
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
        proc = subprocess.run(
            ["pytest", test_path, "-m", markers, "-v"],
            cwd=str(cwd), capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        return {"passed": False, "stdout": "", "stderr": str(exc), "returncode": None}
    return {
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
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
