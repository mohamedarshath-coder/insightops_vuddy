"""Confluence integration for the opsbuddy-fix incident pipeline (Phase 10 postmortem page).

Bash fallback for whichever Confluence steps aren't taken via the Atlassian connector's own
`createConfluencePage`/`updateConfluencePage` MCP tools. Reuses the same Atlassian-account
credentials as workflow/jira_workflow.py where possible (Jira and Confluence Cloud share one
API token per account) -- only CONFLUENCE_BASE_URL genuinely differs from JIRA_BASE_URL (Jira's
own REST root vs. Confluence's `/wiki` root on the same site).

Usage:
  python workflow/confluence_workflow.py upsert-page --space OOP --title "SCRUM-81: ..." \\
      --jira-id SCRUM-81 --run-id 250783835224145 --job-id 536033406198191 \\
      --category "Data Quality / Constraint" --status RESOLVED \\
      --rca "..." --pr-url https://... --branch SCRUM-81/... --verdict PASS \\
      --verification "All 4 tasks TERMINATED/SUCCESS on re-run"
  python workflow/confluence_workflow.py get-page --title "SCRUM-81: ..."
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

import click
import requests
from requests.auth import HTTPBasicAuth
from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python.utils.config import get, require  # noqa: E402
from python.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
console = Console(legacy_windows=False)


def _default_confluence_base_url() -> str:
    # Confluence Cloud lives at "<site>/wiki" on the same Atlassian site as Jira -- reuse
    # JIRA_BASE_URL rather than requiring a second, near-identical URL to be configured.
    jira_base = get("JIRA_BASE_URL", "")
    return f"{jira_base.rstrip('/')}/wiki" if jira_base else ""


class ConfluenceClient:
    def __init__(self) -> None:
        base_url = get("CONFLUENCE_BASE_URL") or _default_confluence_base_url()
        if not base_url:
            raise EnvironmentError(
                "CONFLUENCE_BASE_URL is not set and JIRA_BASE_URL is unset too -- can't derive "
                "a default. Set CONFLUENCE_BASE_URL explicitly, or JIRA_BASE_URL if this site "
                "uses the standard '<site>/wiki' Confluence path."
            )
        self.base_url = base_url.rstrip("/")
        # Same Atlassian API token generally works for both Jira and Confluence on one account --
        # fall back to the Jira credentials rather than forcing a second token to be issued.
        email = get("CONFLUENCE_EMAIL") or require("JIRA_EMAIL")
        token = get("CONFLUENCE_API_TOKEN") or require("JIRA_API_TOKEN")
        self.auth = HTTPBasicAuth(email, token)
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{self.base_url}/rest/api{path}",
            auth=self.auth,
            headers=self.headers,
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/rest/api{path}",
            auth=self.auth,
            headers=self.headers,
            json=data,
        )
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, data: dict) -> dict:
        resp = requests.put(
            f"{self.base_url}/rest/api{path}",
            auth=self.auth,
            headers=self.headers,
            json=data,
        )
        resp.raise_for_status()
        return resp.json()

    def find_page(self, space_key: str, title: str) -> dict | None:
        results = self._get(
            "/content",
            params={"spaceKey": space_key, "title": title, "expand": "version"},
        )
        pages = results.get("results", [])
        return pages[0] if pages else None

    def create_page(self, space_key: str, title: str, body_html: str, parent_id: str = None) -> dict:
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "body": {"storage": {"value": body_html, "representation": "storage"}},
        }
        if parent_id:
            payload["ancestors"] = [{"id": parent_id}]
        page = self._post("/content", payload)
        logger.info(f"Created Confluence page '{title}' (id={page['id']})")
        return page

    def update_page(self, page_id: str, title: str, body_html: str, current_version: int) -> dict:
        page = self._put(
            f"/content/{page_id}",
            {
                "type": "page",
                "title": title,
                "version": {"number": current_version + 1},
                "body": {"storage": {"value": body_html, "representation": "storage"}},
            },
        )
        logger.info(f"Updated Confluence page '{title}' to version {current_version + 1}")
        return page

    def upsert_page(self, space_key: str, title: str, body_html: str, parent_id: str = None) -> dict:
        existing = self.find_page(space_key, title)
        if existing:
            return self.update_page(
                page_id=existing["id"],
                title=title,
                body_html=body_html,
                current_version=existing["version"]["number"],
            )
        return self.create_page(space_key, title, body_html, parent_id)


_STATUS_PANEL = {
    "RESOLVED": ("#36B37E", "#E3FCEF", "✅ RESOLVED"),
    "IN_PROGRESS": ("#0052CC", "#DEEBFF", "\U0001f6e0️ IN PROGRESS"),
    "MANUAL_ACTION_REQUIRED": ("#FF991F", "#FFF7E6", "⚠️ MANUAL ACTION REQUIRED"),
    "REVIEW_FAILED": ("#DE350B", "#FFEBE6", "❌ REVIEW FAILED"),
    "VERIFICATION_FAILED": ("#DE350B", "#FFEBE6", "❌ VERIFICATION FAILED"),
}


def build_incident_page_html(
    jira_id: str,
    job_name: str,
    run_id: str,
    job_id: str,
    error_category: str,
    root_cause_summary: str,
    repo: str,
    branch: str,
    pr_url: str,
    review_verdict: str,
    verification_result: str,
    execution_status: str,
    author: str = "",
    date: str = "",
) -> str:
    if not author:
        author = get("CONFLUENCE_AUTHOR", "opsbuddy-fix")
    if not date:
        date = datetime.now().strftime("%Y-%m-%d %H:%M")

    jira_base = get("JIRA_BASE_URL", "https://your-org.atlassian.net")
    jira_url = f"{jira_base}/browse/{jira_id}" if jira_id else ""

    color, bg, label = _STATUS_PANEL.get(
        execution_status.upper().replace(" ", "_"), ("#42526E", "#F4F5F7", execution_status or "UNKNOWN")
    )

    def row(field: str, value: str) -> str:
        return f"<tr><td><strong>{field}</strong></td><td>{value or '-'}</td></tr>"

    jira_cell = f'<a href="{jira_url}">{jira_id}</a>' if jira_url else (jira_id or "-")
    pr_cell = f'<a href="{pr_url}">{pr_url}</a>' if pr_url else "-"

    return f"""
<ac:structured-macro ac:name="panel">
  <ac:parameter ac:name="borderColor">{color}</ac:parameter>
  <ac:parameter ac:name="bgColor">{bg}</ac:parameter>
  <ac:body><p><strong>{label}</strong> &nbsp;|&nbsp; Last updated {date} &nbsp;|&nbsp; via {author}</p></ac:body>
</ac:structured-macro>

<h1>{jira_id}: {job_name or 'Databricks job'} incident postmortem</h1>

<h2>&#128203; Incident Metadata</h2>
<table><tbody>
<tr><th>Field</th><th>Value</th></tr>
{row("Jira Ticket", jira_cell)}
{row("Job / Run", f"{job_name or '-'} (job {job_id or '-'}, run {run_id or '-'})")}
{row("Error Category", error_category)}
{row("Repo / Branch", f"{repo or '-'} / {branch or '-'}")}
{row("Pull Request", pr_cell)}
{row("Mode A Review Verdict", review_verdict)}
{row("Verification (real re-run)", verification_result)}
{row("Status", label)}
</tbody></table>

<h2>&#128269; Root Cause</h2>
<p>{root_cause_summary or 'No root-cause summary captured.'}</p>

<h2>&#128203; Timeline</h2>
<ol>
<li>Incident detected from failed Databricks run, Jira ticket {jira_id or '(none)'} filed and moved to <em>In Progress</em>.</li>
<li>Root cause diagnosed (adversarial double-check), fix branch <code>{branch or '-'}</code> opened.</li>
<li>Pull request opened: {pr_cell} -- ticket moved to <em>In Review</em>, Slack notified (not yet merged).</li>
<li>Mode A automated review verdict: {review_verdict or '-'}.</li>
<li>PR merged (human approval), Slack notified.</li>
<li>Real re-run triggered to verify the fix in production -- Slack notified while running.</li>
<li>Result: {verification_result or '-'}. Ticket moved to <em>Done</em>, final Slack summary sent.</li>
</ol>

<h2>&#9989; How to Verify</h2>
<ol>
<li>Re-run job {job_id or '(job id)'} and confirm every task reaches <code>TERMINATED</code>/<code>SUCCESS</code>.</li>
<li>Review the merged diff on {pr_cell} against the root cause above.</li>
<li>Check the Databricks incident-log table for the row keyed by <code>{jira_id}</code>.</li>
</ol>

<h2>&#128279; Related Resources</h2>
<ul>
<li>{f'<a href="{jira_url}">Jira Ticket: {jira_id}</a>' if jira_url else 'Jira ticket: (none)'}</li>
<li>{f'<a href="{pr_url}">GitHub Pull Request</a>' if pr_url else 'Pull request: (none)'}</li>
</ul>

<p><em>&#129302; Auto-generated by opsbuddy-fix Phase 10 on {date}.</em></p>
"""


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def cli():
    pass


@cli.command("upsert-page")
@click.option("--space", default=None, help="Confluence space key (default: CONFLUENCE_SPACE_KEY or OOP)")
@click.option("--title", required=True, help="Page title, e.g. 'SCRUM-81: <job_name> incident'")
@click.option("--jira-id", default="", help="Jira ticket key")
@click.option("--job-name", default="", help="Databricks job name")
@click.option("--run-id", default="", help="Databricks run ID")
@click.option("--job-id", default="", help="Databricks job ID")
@click.option("--category", "error_category", default="", help="Error category")
@click.option("--rca", "root_cause_summary", default="", help="Root cause summary")
@click.option("--repo", default="", help="owner/repo")
@click.option("--branch", default="", help="Hotfix branch name")
@click.option("--pr-url", default="", help="Pull request URL")
@click.option("--verdict", "review_verdict", default="", help="Mode A review verdict")
@click.option("--verification", "verification_result", default="", help="Real re-run verification result")
@click.option("--status", "execution_status", default="", help="EXECUTION_STATUS")
@click.option("--parent-id", default=None, help="Parent page ID")
def cmd_upsert_page(
    space,
    title,
    jira_id,
    job_name,
    run_id,
    job_id,
    error_category,
    root_cause_summary,
    repo,
    branch,
    pr_url,
    review_verdict,
    verification_result,
    execution_status,
    parent_id,
):
    """Create or update the incident postmortem page for this run (idempotent by title)."""
    space_key = space or get("CONFLUENCE_SPACE_KEY", "OOP")
    body = build_incident_page_html(
        jira_id=jira_id,
        job_name=job_name,
        run_id=run_id,
        job_id=job_id,
        error_category=error_category,
        root_cause_summary=root_cause_summary,
        repo=repo,
        branch=branch,
        pr_url=pr_url,
        review_verdict=review_verdict,
        verification_result=verification_result,
        execution_status=execution_status,
    )
    page = ConfluenceClient().upsert_page(space_key, title, body, parent_id)
    click.echo(f"[OK] Confluence page upserted: {page.get('_links', {}).get('webui', '')}")


@cli.command("get-page")
@click.option("--space", default=None, help="Confluence space key")
@click.option("--title", required=True, help="Page title")
def cmd_get_page(space, title):
    space_key = space or get("CONFLUENCE_SPACE_KEY", "OOP")
    page = ConfluenceClient().find_page(space_key, title)
    if page:
        click.echo(f"Found page: id={page['id']}, version={page['version']['number']}")
    else:
        click.echo("Page not found")


if __name__ == "__main__":
    cli()
