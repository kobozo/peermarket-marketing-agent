"""Shared pytest fixtures and env scrubbing."""
import os

import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in list(os.environ):
        if var.startswith(("ANTHROPIC_", "SLACK_", "AGENT_DB_", "GITHUB_APP_",
                          "PEERMARKET_PROD_", "RECRAFT_", "RESEND_", "BACKBLAZE_")):
            monkeypatch.delenv(var, raising=False)
    yield
