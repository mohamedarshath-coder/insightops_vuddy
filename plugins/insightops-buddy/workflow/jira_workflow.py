"""Jira integration for the AI-SDLC workflow.

Usage:
  python workflow/jira_workflow.py get-ticket DATA-123
  python workflow/jira_workflow.py transition DATA-123 "In Progress"
  python workflow/jira_workflow.py comment DATA-123 "PR #42 merged"
  python workflow/jira_workflow.py list-tickets --status "To Do"
  python workflow/jira_workflow.py create --project OPS --type Incident --summary "..." --description "..."
  python workflow/jira_workflow.py comment-rich OPS-1 "PR opened" --link pr=https://github.com/org/repo/pull/1
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Optional

import click
import requests
from requests.auth import HTTPBasicAuth
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python.utils.config import get, require  # noqa: E402
from python.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)
# legacy_windows=False: Rich's default legacy-Windows renderer writes via a raw Win32 console
# API call that encodes with the console's codepage (cp1252 here) regardless of stdout's own
# encoding, which crashes on any non-ASCII character a real Jira ticket can easily contain
# (e.g. an em dash or arrow in a summary/description). Forcing the standard ANSI path instead
# makes output encoding follow normal stdout/PYTHONIOENCODING rules.
console = Console(legacy_windows=False)


@dataclass
class JiraTicket:
    key: str
    summary: str
    description: str
    status: str
    priority: str
    assignee: str
    labels: list[str]
    components: list[str]
    acceptance_criteria: str
    story_points: Optional[int]
    sprint: Optional[str]

    def __str__(self) -> str:
        return (
            f"\n{'='*60}\n"
            f"Ticket  : {self.key}\n"
            f"Summary : {self.summary}\n"
            f"Status  : {self.status}\n"
            f"Priority: {self.priority}\n"
            f"Assignee: {self.assignee}\n"
            f"Labels  : {', '.join(self.labels)}\n"
            f"Story Pts: {self.story_points}\n"
            f"\nDescription:\n{self.description}\n"
            f"\nAcceptance Criteria:\n{self.acceptance_criteria}\n"
            f"{'='*60}\n"
        )


class JiraClient:
    def __init__(self) -> None:
        self.base_url = require("JIRA_BASE_URL").rstrip("/")
        self.auth = HTTPBasicAuth(require("JIRA_EMAIL"), require("JIRA_API_TOKEN"))
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f"{self.base_url}/rest/api/3{path}",
            auth=self.auth,
            headers=self.headers,
            params=params or {},
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/rest/api/3{path}",
            auth=self.auth,
            headers=self.headers,
            json=data,
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    def get_ticket(self, ticket_id: str) -> JiraTicket:
        data = self._get(f"/issue/{ticket_id}")
        fields = data["fields"]

        description_text = ""
        if fields.get("description"):
            description_text = _extract_adf_text(fields["description"])

        acceptance_criteria = ""
        for field_name in [
            "customfield_10016",
            "customfield_10014",
            "customfield_10028",
        ]:
            if fields.get(field_name) and isinstance(fields[field_name], dict):
                acceptance_criteria = _extract_adf_text(fields[field_name])
                break

        return JiraTicket(
            key=data["key"],
            summary=fields.get("summary", ""),
            description=description_text,
            status=fields["status"]["name"],
            priority=fields.get("priority", {}).get("name", "Medium"),
            assignee=(
                fields.get("assignee", {}).get("displayName", "Unassigned")
                if fields.get("assignee")
                else "Unassigned"
            ),
            labels=fields.get("labels", []),
            components=[c["name"] for c in fields.get("components", [])],
            acceptance_criteria=acceptance_criteria,
            story_points=fields.get("story_points") or fields.get("customfield_10016"),
            sprint=None,
        )

    def transition_ticket(self, ticket_id: str, transition_name: str) -> None:
        transitions = self._get(f"/issue/{ticket_id}/transitions")
        matched = next(
            (
                t
                for t in transitions["transitions"]
                if t["name"].lower() == transition_name.lower()
            ),
            None,
        )
        if not matched:
            available = [t["name"] for t in transitions["transitions"]]
            raise ValueError(
                f"Transition '{transition_name}' not found. Available: {available}"
            )
        self._post(
            f"/issue/{ticket_id}/transitions", {"transition": {"id": matched["id"]}}
        )
        logger.info(f"Transitioned {ticket_id} -> {transition_name}")

    def add_comment(self, ticket_id: str, comment: str) -> None:
        self._post(
            f"/issue/{ticket_id}/comment",
            {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment}],
                        }
                    ],
                }
            },
        )
        logger.info(f"Comment added to {ticket_id}")

    def get_issue_types(self, project_key: str) -> list[str]:
        data = self._get(f"/issue/createmeta/{project_key}/issuetypes")
        return [t["name"] for t in data.get("issueTypes", [])]

    def create_ticket(
        self,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        priority: str = "High",
        labels: Optional[list[str]] = None,
    ) -> str:
        available = self.get_issue_types(project_key)
        if available and issue_type not in available:
            fallback = next(
                (t for t in ("Incident", "Bug", "Task", "Story") if t in available),
                available[0],
            )
            logger.warning(
                f"Issue type '{issue_type}' not available in project {project_key} "
                f"(available: {available}); falling back to '{fallback}'"
            )
            issue_type = fallback

        payload = {
            "fields": {
                "project": {"key": project_key},
                "issuetype": {"name": issue_type},
                "summary": summary,
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": description}],
                        }
                    ],
                },
                "priority": {"name": priority},
                "labels": labels or [],
            }
        }
        data = self._post("/issue", payload)
        ticket_key = data["key"]
        logger.info(f"Created Jira ticket {ticket_key}")
        return ticket_key

    def add_comment_with_links(
        self, ticket_id: str, text: str, links: Optional[dict[str, str]] = None
    ) -> None:
        content = [{"type": "text", "text": text}]
        for label, url in (links or {}).items():
            content.append({"type": "text", "text": "\n"})
            content.append(
                {
                    "type": "text",
                    "text": f"{label}: {url}",
                    "marks": [{"type": "link", "attrs": {"href": url}}],
                }
            )
        self._post(
            f"/issue/{ticket_id}/comment",
            {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [{"type": "paragraph", "content": content}],
                }
            },
        )
        logger.info(f"Rich comment added to {ticket_id}")

    def list_tickets(self, status: str = None, project: str = None) -> list[JiraTicket]:
        project_key = project or get("JIRA_PROJECT_KEY", "SCRUM")
        jql = f"project = {project_key}"
        if status:
            jql += f' AND status = "{status}"'
        jql += " ORDER BY priority ASC"

        # `/search/jql` only returns `id` unless `fields` is requested explicitly.
        data = self._get(
            "/search/jql", params={"jql": jql, "maxResults": 50, "fields": "key"}
        )
        return [self.get_ticket(issue["key"]) for issue in data.get("issues", [])]

    def find_existing_incident(self, project_key: str, run_id: str) -> Optional[str]:
        """Look for an already-open opsbuddy-fix ticket for this exact run ID, so
        re-running the pipeline on the same failure doesn't create a duplicate ticket.
        """
        # Don't require an "opsbuddy-fix" label -- a ticket for this run may have been created
        # by a different path (e.g. a direct Atlassian MCP call) that never applied one. A
        # Databricks run ID is specific enough on its own to search on without false positives.
        jql = (
            f'project = {project_key} AND text ~ "{run_id}" AND statusCategory != Done'
        )
        # `/search/jql` only returns `id` unless `fields` is requested explicitly.
        data = self._get(
            "/search/jql", params={"jql": jql, "maxResults": 1, "fields": "key"}
        )
        issues = data.get("issues", [])
        return issues[0]["key"] if issues else None


def _extract_adf_text(node: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [_extract_adf_text(child) for child in node.get("content", [])]
    return "\n".join(filter(None, parts))


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def cli():
    pass


@cli.command("get-ticket")
@click.argument("ticket_id")
def cmd_get_ticket(ticket_id: str):
    """Fetch and display a Jira ticket."""
    client = JiraClient()
    ticket = client.get_ticket(ticket_id)
    console.print(str(ticket))
    # Also output structured JSON for programmatic use
    print(
        json.dumps(
            {
                "key": ticket.key,
                "summary": ticket.summary,
                "description": ticket.description,
                "status": ticket.status,
                "labels": ticket.labels,
                "acceptance_criteria": ticket.acceptance_criteria,
            }
        )
    )


@cli.command("transition")
@click.argument("ticket_id")
@click.argument("transition_name")
def cmd_transition(ticket_id: str, transition_name: str):
    """Move a ticket to a new status."""
    JiraClient().transition_ticket(ticket_id, transition_name)
    click.echo(f"[OK] {ticket_id} -> {transition_name}")


@cli.command("comment")
@click.argument("ticket_id")
@click.argument("comment")
def cmd_comment(ticket_id: str, comment: str):
    """Add a comment to a Jira ticket."""
    JiraClient().add_comment(ticket_id, comment)
    click.echo(f"[OK] Comment added to {ticket_id}")


@cli.command("create")
@click.option(
    "--project",
    "project_key",
    default=None,
    help="Project key (default: JIRA_OPS_PROJECT_KEY)",
)
@click.option("--type", "issue_type", default="Incident", help="Issue type name")
@click.option("--summary", required=True, help="Ticket summary")
@click.option("--description", required=True, help="Ticket description")
@click.option("--priority", default="High", help="Priority name")
@click.option("--label", "labels", multiple=True, help="Label (repeatable)")
def cmd_create(
    project_key: Optional[str],
    issue_type: str,
    summary: str,
    description: str,
    priority: str,
    labels: tuple,
):
    """Create a new Jira ticket."""
    key = project_key or get("JIRA_OPS_PROJECT_KEY", "OPS")
    ticket_key = JiraClient().create_ticket(
        project_key=key,
        issue_type=issue_type,
        summary=summary,
        description=description,
        priority=priority,
        labels=list(labels),
    )
    click.echo(f"[OK] Created {ticket_key}")


@cli.command("comment-rich")
@click.argument("ticket_id")
@click.argument("comment")
@click.option("--link", "links", multiple=True, help="label=url (repeatable)")
def cmd_comment_rich(ticket_id: str, comment: str, links: tuple):
    """Add a comment with one or more labeled links to a Jira ticket."""
    link_map = dict(link.split("=", 1) for link in links)
    JiraClient().add_comment_with_links(ticket_id, comment, links=link_map)
    click.echo(f"[OK] Rich comment added to {ticket_id}")


@cli.command("list-tickets")
@click.option("--status", default=None, help="Filter by status")
@click.option("--project", default=None, help="Project key")
def cmd_list_tickets(status: Optional[str], project: Optional[str]):
    """List tickets from the project."""
    tickets = JiraClient().list_tickets(status=status, project=project)
    table = Table(title="Jira Tickets")
    table.add_column("Key", style="cyan")
    table.add_column("Summary")
    table.add_column("Status", style="yellow")
    table.add_column("Priority")
    for t in tickets:
        table.add_row(t.key, t.summary[:60], t.status, t.priority)
    console.print(table)


@cli.command("find-incident")
@click.option("--project", required=True, help="Project key to search")
@click.option("--run-id", required=True, help="Databricks run ID to search for")
def cmd_find_incident(project: str, run_id: str):
    """Check whether an open opsbuddy-fix ticket already exists for this run ID."""
    existing = JiraClient().find_existing_incident(project, run_id)
    if existing:
        click.echo(existing)
    else:
        click.echo("")


@cli.command("check-access")
@click.option(
    "--project", required=True, help="Project key to verify (e.g. OPS, SCRUM)"
)
def cmd_check_access(project: str):
    """Verify the project exists and list its available issue types (fast preflight check)."""
    client = JiraClient()
    issue_types = client.get_issue_types(project)
    if not issue_types:
        click.echo(f"[FAIL] Project '{project}' not found or has no issue types")
        raise SystemExit(1)
    click.echo(f"[OK] Project '{project}' found. Issue types: {', '.join(issue_types)}")


if __name__ == "__main__":
    cli()
