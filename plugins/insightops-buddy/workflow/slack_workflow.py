"""Slack alerting for the opsbuddy-fix incident pipeline (Phase 10).

Usage:
  python workflow/slack_workflow.py send --text "..."
  python workflow/slack_workflow.py send-incident-summary --jira-id OPS-1 \\
      --run-id 48213 --category "Schema Mismatch" --pr-url https://... --verdict PASS --status RESOLVED
"""

from __future__ import annotations

import os
import sys
from typing import Optional

import click
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from python.utils.config import require  # noqa: E402
from python.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


class SlackClient:
    def __init__(self) -> None:
        self.webhook_url = require("SLACK_WEBHOOK_URL")

    def send_message(self, text: str, blocks: Optional[list] = None) -> None:
        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        response = requests.post(self.webhook_url, json=payload, timeout=10)
        if response.status_code != 200:
            raise RuntimeError(
                f"Slack webhook returned {response.status_code}: {response.text}"
            )
        logger.info(f"Sent Slack message: {text}")


def build_incident_summary_blocks(incident: dict) -> list:
    fields = [
        {"type": "mrkdwn", "text": f"*{key}*\n{value or '-'}"}
        for key, value in incident.items()
    ]
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "opsbuddy-fix incident summary"},
        },
        {"type": "section", "fields": fields},
    ]


# ── CLI ───────────────────────────────────────────────────────────────────────


@click.group()
def cli():
    pass


@cli.command("send")
@click.option(
    "--text", required=True, help="Message text (also used as notification fallback)"
)
def cmd_send(text: str):
    """Send an arbitrary Slack message."""
    SlackClient().send_message(text=text)
    click.echo("[OK] Slack message sent")


@cli.command("send-incident-summary")
@click.option("--jira-id", default="")
@click.option("--run-id", default="")
@click.option("--category", default="")
@click.option("--pr-url", default="")
@click.option("--verdict", default="")
@click.option("--status", "execution_status", default="")
def cmd_send_incident_summary(
    jira_id: str,
    run_id: str,
    category: str,
    pr_url: str,
    verdict: str,
    execution_status: str,
):
    """Send the standard opsbuddy-fix Phase 10 Slack incident summary."""
    incident = {
        "Jira Ticket": jira_id,
        "Databricks Run ID": run_id,
        "Error Category": category,
        "PR": pr_url,
        "Review Verdict": verdict,
        "Execution Status": execution_status,
    }
    blocks = build_incident_summary_blocks(incident)
    text = f"[opsbuddy-fix] {jira_id or run_id} — {execution_status or 'update'}"
    SlackClient().send_message(text=text, blocks=blocks)
    click.echo("[OK] Incident summary sent to Slack")


if __name__ == "__main__":
    cli()
