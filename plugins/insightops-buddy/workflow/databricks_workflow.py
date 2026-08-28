"""Databricks job-run failure telemetry for the opsbuddy-fix incident pipeline.

Usage:
  python workflow/databricks_workflow.py get-run-failure --run-id 48213
  python workflow/databricks_workflow.py get-latest-failed-run --job-id 501
  python workflow/databricks_workflow.py log-incident --json-file incident.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

import click
from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python.utils.config import get, require  # noqa: E402
from python.utils.databricks_conn import insert_ops_incident_log  # noqa: E402
from python.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
console = Console()

FAILED_RESULT_STATES = ("FAILED", "TIMEDOUT", "CANCELED")
TERMINAL_LIFE_CYCLE_STATES = ("TERMINATED", "SKIPPED", "INTERNAL_ERROR")


@dataclass
class RunOutcome:
    run_id: int
    life_cycle_state: str
    result_state: str
    run_page_url: str
    succeeded: bool


@dataclass
class RepoMapping:
    repo_url: Optional[str]
    branch: Optional[str]
    relative_path_in_repo: Optional[str]
    error: Optional[str]


@dataclass
class JobRunFailure:
    job_id: Optional[int]
    run_id: int
    job_name: str
    task_key: str
    life_cycle_state: str
    result_state: str
    error_message: str
    stack_trace: str
    cluster_id: Optional[str]
    run_page_url: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    parameters: dict

    def __str__(self) -> str:
        return (
            f"\n{'='*60}\n"
            f"Job     : {self.job_name} (job_id={self.job_id})\n"
            f"Run     : {self.run_id} / task {self.task_key}\n"
            f"State   : {self.life_cycle_state} / {self.result_state}\n"
            f"URL     : {self.run_page_url}\n"
            f"\nError:\n{self.error_message}\n"
            f"\nStack trace:\n{self.stack_trace}\n"
            f"{'='*60}\n"
        )


class DatabricksClient:
    def __init__(self) -> None:
        from databricks.sdk import WorkspaceClient

        self.client = WorkspaceClient(
            host=require("DATABRICKS_HOST"),
            token=require("DATABRICKS_TOKEN"),
        )

    def get_run_failure(self, run_id: int) -> JobRunFailure:
        run = self.client.jobs.get_run(run_id=run_id)
        task = _pick_failed_task(run)
        error_message, stack_trace = self._get_task_error(
            task.run_id if task else run_id
        )

        state = run.state
        return JobRunFailure(
            job_id=run.job_id,
            run_id=run.run_id,
            job_name=run.run_name or "",
            task_key=task.task_key if task else "",
            life_cycle_state=(
                state.life_cycle_state.value
                if state and state.life_cycle_state
                else "UNKNOWN"
            ),
            result_state=(
                state.result_state.value if state and state.result_state else "-"
            ),
            error_message=error_message,
            stack_trace=stack_trace,
            cluster_id=getattr(task, "existing_cluster_id", None) if task else None,
            run_page_url=run.run_page_url or "",
            start_time=_to_datetime(run.start_time),
            end_time=_to_datetime(run.end_time),
            parameters=_extract_parameters(run),
        )

    def get_latest_failed_run(self, job_id: int) -> int:
        for run in self.client.jobs.list_runs(
            job_id=job_id, active_only=False, limit=25
        ):
            state = run.state
            result_state = (
                state.result_state.value if state and state.result_state else None
            )
            if result_state in FAILED_RESULT_STATES:
                return run.run_id
        raise SystemExit(f"No failed runs found for job {job_id}")

    def _get_task_error(self, task_run_id: int) -> tuple[str, str]:
        try:
            output = self.client.jobs.get_run_output(run_id=task_run_id)
        except Exception as exc:  # SDK/network edge cases — degrade gracefully
            logger.warning(f"Could not fetch run output for {task_run_id}: {exc}")
            return "", ""
        return output.error or "", output.error_trace or ""

    def get_repo_mapping(
        self, source_path: str, job_id: Optional[int] = None
    ) -> RepoMapping:
        """Resolve a workspace source_path to the GitHub repo it's actually backed by, so
        opsbuddy-fix commits a fix against the real repo instead of assuming GITHUB_REPO.

        Two mechanisms, tried in order:
        1. source_path under /Repos/... -- matched against a live Databricks Repo checkout
           to get its git remote URL and branch.
        2. Otherwise, if job_id is given -- checked against that job's job-level git_source,
           used when a job runs straight from a Git repo without a Repos checkout.

        Always pass job_id when you have it -- mechanism 2 needs it and mechanism 1 doesn't
        use it, so passing it costs nothing.
        """
        if source_path.startswith("/Repos/"):
            for repo in self.client.repos.list():
                if repo.path and source_path.startswith(repo.path + "/"):
                    prefix_len = len(repo.path) + 1
                    return RepoMapping(
                        repo_url=repo.url,
                        branch=repo.branch,
                        relative_path_in_repo=source_path[prefix_len:],
                        error=None,
                    )
            return RepoMapping(
                repo_url=None,
                branch=None,
                relative_path_in_repo=None,
                error=f"{source_path} is under /Repos/ but no matching Repo checkout was found.",
            )

        if job_id:
            job = self.client.jobs.get(job_id=job_id)
            git_source = (
                getattr(job.settings, "git_source", None) if job.settings else None
            )
            git_url = getattr(git_source, "git_url", None) if git_source else None
            if git_url:
                branch = getattr(git_source, "git_branch", None) or getattr(
                    git_source, "git_tag", None
                )
                return RepoMapping(
                    repo_url=git_url,
                    branch=branch,
                    relative_path_in_repo=source_path,
                    error=None,
                )

        return RepoMapping(
            repo_url=None,
            branch=None,
            relative_path_in_repo=None,
            error=(
                f"{source_path} is not under /Repos/ and job {job_id or '(none given)'} "
                "has no git_source configured -- this is a workspace-native file/notebook "
                "with no git repo backing it. There is no repo to open a PR against."
            ),
        )

    def sync_repo(self, repo_url: str, branch: str) -> str:
        """Point the workspace's Databricks Repo for `repo_url` at `branch`'s latest
        commit. Databricks Repos does NOT auto-sync on a GitHub push — this must be
        called explicitly before re-running a job against newly-pushed code.
        """
        me = self.client.current_user.me()
        repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1]
        if repo_name.endswith(".git"):
            repo_name = repo_name[: -len(".git")]
        path = f"/Repos/{me.user_name}/{repo_name}"

        status = self.client.workspace.get_status(path)
        self.client.repos.update(repo_id=status.object_id, branch=branch)
        updated = self.client.repos.get(repo_id=status.object_id)
        logger.info(f"Synced {path} to {branch} @ {updated.head_commit_id}")
        return updated.head_commit_id

    def trigger_and_wait(
        self,
        job_id: int,
        timeout_seconds: int = 600,
        poll_interval: int = 10,
        force: bool = False,
    ) -> RunOutcome:
        """Re-run a persistent job and block until it reaches a terminal state.

        Used to confirm a hotfix actually resolves the original failure, rather than
        trusting a code review alone. Requires a persistent job_id (from `jobs.create`
        / a scheduled job) — one-time `jobs.submit()` runs have no job to re-trigger.

        Real production jobs can write real data — re-running one against an unmerged
        hotfix branch before a human reviews it can mutate production state. Unless
        `job_id` is in `OPSBUDDY_VERIFY_ALLOWLIST` (or that's set to "all"), this
        refuses to run unless `force=True` is passed — which should only happen after
        a human has explicitly approved it.
        """
        if not force and not is_verify_allowed(job_id):
            raise PermissionError(
                f"job_id {job_id} is not in OPSBUDDY_VERIFY_ALLOWLIST. Re-running a "
                "real job against unreviewed, unmerged code needs explicit human "
                "approval first — this is not something to auto-approve. Once a human "
                "has approved it, retry with force=True (CLI: --force)."
            )
        run = self.client.jobs.run_now(job_id=job_id)
        run_id = run.run_id
        elapsed = 0
        while elapsed < timeout_seconds:
            run = self.client.jobs.get_run(run_id=run_id)
            state = run.state
            life_cycle = (
                state.life_cycle_state.value
                if state and state.life_cycle_state
                else "UNKNOWN"
            )
            result_state = (
                state.result_state.value if state and state.result_state else "-"
            )
            if life_cycle in TERMINAL_LIFE_CYCLE_STATES:
                return RunOutcome(
                    run_id=run_id,
                    life_cycle_state=life_cycle,
                    result_state=result_state,
                    run_page_url=run.run_page_url or "",
                    succeeded=result_state == "SUCCESS",
                )
            time.sleep(poll_interval)
            elapsed += poll_interval
        raise TimeoutError(f"Run {run_id} did not finish within {timeout_seconds}s")


def is_verify_allowed(job_id: int) -> bool:
    """Only jobs explicitly opted in via OPSBUDDY_VERIFY_ALLOWLIST (comma-separated
    job IDs, or "all" — use "all" only in a sandbox/non-production workspace) may be
    auto-re-run by Gate 8.5. Everything else needs a human's explicit approval.
    """
    allowlist = get("OPSBUDDY_VERIFY_ALLOWLIST", "")
    if allowlist.strip().lower() == "all":
        return True
    allowed_ids = {x.strip() for x in allowlist.split(",") if x.strip()}
    return str(job_id) in allowed_ids


def _pick_failed_task(run):
    tasks = run.tasks or []
    for task in tasks:
        state = task.state
        if (
            state
            and state.result_state
            and state.result_state.value in FAILED_RESULT_STATES
        ):
            return task
    return tasks[0] if tasks else None


def _to_datetime(epoch_ms: Optional[int]) -> Optional[datetime]:
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000)


def _extract_parameters(run) -> dict:
    params: dict = {}
    for task in run.tasks or []:
        notebook_task = getattr(task, "notebook_task", None)
        if notebook_task and notebook_task.base_parameters:
            params.update(notebook_task.base_parameters)
    return params


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def cli():
    pass


@cli.command("get-run-failure")
@click.option("--run-id", required=True, type=int, help="Databricks job run ID")
def cmd_get_run_failure(run_id: int):
    """Fetch failure telemetry (stack trace, cluster, params) for a job run."""
    failure = DatabricksClient().get_run_failure(run_id)
    console.print(str(failure))
    payload = asdict(failure)
    payload["start_time"] = str(failure.start_time) if failure.start_time else None
    payload["end_time"] = str(failure.end_time) if failure.end_time else None
    print(json.dumps(payload))


@cli.command("get-latest-failed-run")
@click.option("--job-id", required=True, type=int, help="Databricks job ID")
def cmd_get_latest_failed_run(job_id: int):
    """Resolve the most recent failed run ID for a job."""
    run_id = DatabricksClient().get_latest_failed_run(job_id)
    click.echo(run_id)


@cli.command("get-repo-mapping")
@click.option(
    "--source-path", required=True, help="Workspace source_path of the failed task"
)
@click.option(
    "--job-id",
    type=int,
    default=None,
    help="Job ID, for job-level git_source resolution",
)
def cmd_get_repo_mapping(source_path: str, job_id: Optional[int]):
    """Resolve a workspace source_path to the GitHub repo it's actually backed by."""
    mapping = DatabricksClient().get_repo_mapping(source_path, job_id=job_id)
    print(json.dumps(asdict(mapping)))
    if mapping.error:
        raise SystemExit(1)


@cli.command("sync-repo")
@click.option(
    "--repo-url", required=True, help="GitHub repo URL backing the Databricks Repo"
)
@click.option("--branch", default="main", help="Branch to sync to")
def cmd_sync_repo(repo_url: str, branch: str):
    """Pull the latest commit into the linked Databricks Repo (no auto-sync on push)."""
    head_commit = DatabricksClient().sync_repo(repo_url, branch)
    click.echo(f"[OK] Synced to {branch} @ {head_commit}")


@cli.command("trigger-and-wait")
@click.option(
    "--job-id", required=True, type=int, help="Persistent Databricks job ID to re-run"
)
@click.option("--timeout", "timeout_seconds", default=600, help="Max seconds to wait")
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Bypass OPSBUDDY_VERIFY_ALLOWLIST — only after a human has explicitly approved this run",
)
def cmd_trigger_and_wait(job_id: int, timeout_seconds: int, force: bool):
    """Re-run a job and block until it succeeds or fails — used to verify a hotfix for real."""
    outcome = DatabricksClient().trigger_and_wait(
        job_id, timeout_seconds=timeout_seconds, force=force
    )
    status = "SUCCEEDED" if outcome.succeeded else "FAILED"
    click.echo(
        f"[{status}] run {outcome.run_id}: {outcome.life_cycle_state}/{outcome.result_state}"
    )
    click.echo(outcome.run_page_url)
    if not outcome.succeeded:
        raise SystemExit(1)


@cli.command("log-incident")
@click.option(
    "--json-file",
    required=True,
    type=click.Path(exists=True),
    help="Path to an incident record JSON file",
)
def cmd_log_incident(json_file: str):
    """Write a structured incident row into the Databricks ops incident log."""
    with open(json_file, "r", encoding="utf-8") as f:
        record = json.load(f)
    insert_ops_incident_log(record)
    click.echo(f"[OK] Incident logged: {record.get('incident_id')}")


if __name__ == "__main__":
    cli()
