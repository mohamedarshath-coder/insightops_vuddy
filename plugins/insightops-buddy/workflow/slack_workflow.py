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

from python.utils.config import get, require  # noqa: E402
from python.utils.logger import get_logger  # noqa: E402

logger = get_logger(__name__)


class SlackClient:
    """Prefers the Slack Web API (SLACK_BOT_TOKEN + SLACK_CHANNEL_ID) over the plain incoming
    webhook whenever both are configured -- only the Web API path can thread a reply under an
    earlier message (chat.postMessage returns a `ts` a later call can pass back as `thread_ts`;
    an incoming webhook has no way to return the posted message's identity at all). Falls back to
    SLACK_WEBHOOK_URL, with no threading, if the bot token isn't set -- same behavior as before
    this was added."""

    def __init__(self) -> None:
        self.bot_token = get("SLACK_BOT_TOKEN", "")
        self.channel_id = get("SLACK_CHANNEL_ID", "")
        self.webhook_url = get("SLACK_WEBHOOK_URL", "") if not (self.bot_token and self.channel_id) else ""
        if not self.webhook_url and not (self.bot_token and self.channel_id):
            # Neither path configured -- surface the same clear error `require` would have given
            # for the simpler pre-bot-token setup.
            require("SLACK_WEBHOOK_URL")

    def send_message(self, text: str, blocks: Optional[list] = None, thread_ts: str = "") -> dict:
        """Returns {"ts": ..., "channel": ...} (both None on the webhook path -- nothing to
        thread into later)."""
        if self.bot_token and self.channel_id:
            payload = {"channel": self.channel_id, "text": text}
            if blocks:
                payload["blocks"] = blocks
            if thread_ts:
                payload["thread_ts"] = thread_ts
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.bot_token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json=payload,
                timeout=10,
            )
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(f"Slack API error: {data.get('error')}")
            logger.info(f"Sent Slack message (thread_ts={data.get('ts')}): {text}")
            return {"ts": data.get("ts"), "channel": data.get("channel")}

        payload = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        response = requests.post(self.webhook_url, json=payload, timeout=10)
        if response.status_code != 200:
            raise RuntimeError(
                f"Slack webhook returned {response.status_code}: {response.text}"
            )
        logger.info(f"Sent Slack message: {text}")
        return {"ts": None, "channel": None}


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
@click.option("--job-id", default="", help="The Databricks job being fixed -- stays the same across all five checkpoints of one incident, unlike --run-id.")
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
@click.option(
    "--thread-ts",
    default="",
    help=(
        "Reply into this thread instead of posting a new top-level message -- pass the ts a "
        "previous call for this same incident printed (only possible on the SLACK_BOT_TOKEN + "
        "SLACK_CHANNEL_ID path; ignored, since there's nothing to thread into, on the plain "
        "SLACK_WEBHOOK_URL path). Omit on the first call (stage=incident_detected) -- that one "
        "is the thread's parent."
    ),
)
def cmd_send_incident_summary(
    jira_id: str,
    job_id: str,
    run_id: str,
    category: str,
    pr_url: str,
    verdict: str,
    execution_status: str,
    stage: str,
    message: str,
    thread_ts: str,
):
    """Send one opsbuddy-fix incident checkpoint. Call up to five times per run (--stage)."""
    incident = {
        "Jira Ticket": jira_id,
        "Job ID": job_id,
        "Databricks Run ID": run_id,
        "Error Category": category,
        "PR": pr_url,
        "Review Verdict": verdict,
        "Execution Status": execution_status,
    }
    blocks = build_incident_summary_blocks(incident, stage=stage, message=message)
    text = f"[opsbuddy-fix] {jira_id or run_id} — {stage or execution_status or 'update'}"
    result = SlackClient().send_message(text=text, blocks=blocks, thread_ts=thread_ts)
    click.echo("[OK] Incident summary sent to Slack")
    if result.get("ts"):
        click.echo(
            f"THREAD_TS={result['ts']} -- pass this as --thread-ts on this incident's next "
            "checkpoint call to keep replying in the same thread."
        )


if __name__ == "__main__":
    cli()
