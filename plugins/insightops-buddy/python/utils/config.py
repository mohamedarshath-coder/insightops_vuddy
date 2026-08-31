import os
from pathlib import Path

from dotenv import load_dotenv

_loaded = False
_ca_bundle_ensured = False


def load_config() -> None:
    global _loaded
    if _loaded:
        return
    env_path = Path(__file__).parents[2] / ".env"
    if env_path.exists():
        # override=True: without it, python-dotenv silently skips any key already present in
        # os.environ, even a stale/placeholder one set by something else in the shell -- this
        # repo's own .env should always win.
        load_dotenv(env_path, override=True)
    _loaded = True
    ensure_ca_bundle()


def ensure_ca_bundle() -> None:
    """Behind a TLS-intercepting corporate proxy (e.g. Zscaler), every workflow script's plain
    `requests` calls -- Jira, Confluence, Slack, and PyGithub's own `requests` calls under the
    hood -- fail outright with SSLCertVerificationError even with fully correct credentials.
    Confirmed in practice for mcp-server/server.py's create_pr/post_slack_alert (that's where
    this logic was first written); this is the same fix generalized to every workflow/*.py
    script, since none of them had it and slack_workflow.py hit exactly this error on a live
    test. Called automatically from load_config() -- every script gets it for free the moment it
    calls require()/get(), no per-script wiring needed.

    If OPSBUDDY_EXTRA_CA_CERT/NODE_EXTRA_CA_CERTS points at the proxy's root CA, build a combined
    bundle (the normal public CA bundle plus that extra cert) once and point REQUESTS_CA_BUNDLE
    at it -- `requests` (and anything built on it, including PyGithub) picks that env var up
    automatically. No-op if neither var is set, the file doesn't exist, or REQUESTS_CA_BUNDLE is
    already set explicitly (never override an explicit choice). Keep in sync with
    mcp-server/server.py's own `_ensure_ca_bundle` if this logic changes."""
    global _ca_bundle_ensured
    if _ca_bundle_ensured:
        return
    _ca_bundle_ensured = True
    extra_cert = (
        os.environ.get("OPSBUDDY_EXTRA_CA_CERT", "").strip()
        or os.environ.get("NODE_EXTRA_CA_CERTS", "").strip()
    )
    if not extra_cert or not os.path.exists(extra_cert):
        return
    if os.environ.get("REQUESTS_CA_BUNDLE"):
        return
    import tempfile

    combined_path = Path(tempfile.gettempdir()) / "opsbuddy-fix-combined-ca-bundle.pem"
    if not combined_path.exists():
        import certifi

        with open(certifi.where(), "r", encoding="utf-8") as f:
            base_bundle = f.read()
        with open(extra_cert, "r", encoding="utf-8") as f:
            extra = f.read()
        combined_path.write_text(base_bundle + "\n" + extra, encoding="utf-8")
    os.environ["REQUESTS_CA_BUNDLE"] = str(combined_path)


def require(key: str) -> str:
    load_config()
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. Check your .env file."
        )
    return value


def get(key: str, default: str = "") -> str:
    load_config()
    return os.getenv(key, default)
