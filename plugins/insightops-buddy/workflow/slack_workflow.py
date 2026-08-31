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


# Five checkpoints across the pipeline -- see cmd_send_incident_summary's --stage help for when
# each fires. Kept in sync with mcp-server/server.py's _STAGE_HEADERS (same dict, same keys).
STAGE_HEADERS = {
    "incident_detected": "\U0001f6a8 opsbuddy-fix -- incident detected",
    "pr_opened": "\U0001f500 opsbuddy-fix -- PR opened (not yet merged)",
    "pr_merged": "✅ opsbuddy-fix -- PR merged",
    "verification_running": "⏳ opsbuddy-fix -- verifying fix (re-run in progress)",
    "resolved": "\U0001f389 opsbuddy-fix -- incident resolved",
}


def build_incident_summary_blocks(incident: dict, stage: str = "", message: str = "") -> list:
    header_text = STAGE_HEADERS.get(stage, "opsbuddy-fix incident summary")
    fields = [
        {"type": "mrkdwn", "text": f"*{key}*\n{value or '-'}"}
        for key, value in incident.items()
    ]
    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text}},
        {"type": "section", "fields": fields},
    ]
    if message:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": message}})
    return blocks


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
@click.option(
    "--stage",
    default="",
    type=click.Choice(
        ["", "incident_detected", "pr_opened", "pr_merged", "verification_running", "resolved"]
    ),
    help=(
        "Which pipeline checkpoint this post is for -- controls the header/emoji only. "
        "incident_detected=Phase 3 (ticket filed; put RCA in --message), pr_opened=Phase 7 "
        "(not yet merged), pr_merged=after the Merge Approval Gate, "
        "verification_running=Gate 8.5 (right before the real re-run), resolved=Phase 10 final "
        "outcome. Omit for the original undifferentiated 'incident summary' header."
    ),
)
@click.option("--message", default="", help="Free-text prose (e.g. RCA summary, verification result)")
def cmd_send_incident_summary(
    jira_id: str,
    run_id: str,
    category: str,
    pr_url: str,
    verdict: str,
    execution_status: str,
    stage: str,
    message: str,
):
    """Send one opsbuddy-fix incident checkpoint. Call up to five times per run (--stage)."""
    incident = {
        "Jira Ticket": jira_id,
        "Databricks Run ID": run_id,
        "Error Category": category,
        "PR": pr_url,
        "Review Verdict": verdict,
        "Execution Status": execution_status,
    }
    blocks = build_incident_summary_blocks(incident, stage=stage, message=message)
    text = f"[opsbuddy-fix] {jira_id or run_id} — {stage or execution_status or 'update'}"
    SlackClient().send_message(text=text, blocks=blocks)
    click.echo("[OK] Incident summary sent to Slack")


if __name__ == "__main__":
    cli()
