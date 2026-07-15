"""Agent startup job isolation tests."""

from unittest.mock import AsyncMock

from peermarket_agent.agent.main import _run_startup_jobs


async def test_startup_runs_kpi_when_outbox_fails(monkeypatch):
    outbox = AsyncMock(side_effect=RuntimeError("slack"))
    pulse = AsyncMock()
    monkeypatch.setattr("peermarket_agent.agent.main.run_slack_outbox", outbox)
    monkeypatch.setattr("peermarket_agent.agent.main.run_hourly_pulse", pulse)

    await _run_startup_jobs(object(), object(), object())

    outbox.assert_awaited_once()
    pulse.assert_awaited_once()


async def test_startup_runs_outbox_when_kpi_fails(monkeypatch):
    calls = []
    outbox = AsyncMock(side_effect=lambda *args: calls.append("outbox"))
    pulse = AsyncMock(side_effect=RuntimeError("kpi"))
    monkeypatch.setattr("peermarket_agent.agent.main.run_slack_outbox", outbox)
    monkeypatch.setattr("peermarket_agent.agent.main.run_hourly_pulse", pulse)

    await _run_startup_jobs(object(), object(), object())

    outbox.assert_awaited_once()
    pulse.assert_awaited_once()
    assert calls == ["outbox"]
