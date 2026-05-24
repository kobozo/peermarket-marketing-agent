"""Shared pytest fixtures and env scrubbing."""

import os

import pytest

# AGENT_DB_URL is test infrastructure config (points at the local
# pgvector container), not a production secret — preserve it across the
# autouse scrub so DB-backed tests can read it from os.environ.
_PRESERVE = {"AGENT_DB_URL"}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for var in list(os.environ):
        if var in _PRESERVE:
            continue
        if var.startswith(
            (
                "ANTHROPIC_",
                "SLACK_",
                "AGENT_DB_",
                "GITHUB_APP_",
                "PEERMARKET_PROD_",
                "RECRAFT_",
                "RESEND_",
                "BACKBLAZE_",
            )
        ):
            monkeypatch.delenv(var, raising=False)
    yield
