"""Deployment workflow contracts that protect runtime Slack configuration."""

from pathlib import Path

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"


def test_deploy_preserves_slack_revision_runtime_contract():
    workflow_text = DEPLOY_WORKFLOW.read_text()
    workflow = yaml.safe_load(workflow_text)

    assert {"test", "deploy"} <= workflow["jobs"].keys()
    assert workflow["jobs"]["deploy"]["needs"] == "test"

    for name in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "SLACK_SIGNING_SECRET",
        "SLACK_FOUNDER_USER_ID",
    ):
        assert f"{name}=${name}" in workflow_text
        assert f"secrets.{name}" in workflow_text

    assert "marketing-agent.service slack-bridge.service" in workflow_text
    assert "systemd-run" not in workflow_text
    shell_text = " ".join(workflow_text.replace("\\\n", " ").split())
    assert "sudo -u peermarket-agent bash -c" in shell_text
    migrate = workflow_text.index("/opt/peermarket-agent/.venv/bin/peermarket-migrate")
    restart = workflow_text.index("sudo systemctl restart")
    assert migrate < restart
    migration_block = workflow_text[workflow_text.rfind("run: |", 0, migrate) : restart]
    assert "set -euo pipefail" in migration_block
    assert "set -a" in migration_block
    assert "source /etc/peermarket-agent/secrets.env" in migration_block
    assert "set +a" in migration_block
    assert "cd /opt/peermarket-agent" in migration_block
    assert "sudo -u peermarket-agent bash -c" in migration_block
    assert "||" not in migration_block


def test_deploy_wires_disabled_first_performance_variables():
    workflow_text = DEPLOY_WORKFLOW.read_text()
    defaults = {
        "META_INSIGHTS_ENABLED": "false",
        "PEERMARKET_ATTRIBUTION_ENABLED": "false",
        "META_INSIGHTS_LOOKBACK_DAYS": "3",
        "META_NO_DELIVERY_GRACE_HOURS": "2",
        "LEARNING_MIN_IMPRESSIONS": "1000",
        "LEARNING_MIN_LANDING_PAGE_VIEWS": "30",
        "LEARNING_MIN_REGISTRATIONS": "10",
    }

    for name, default in defaults.items():
        assert f"{name}: ${{{{ vars.{name} || '{default}' }}}}" in workflow_text
        assert f"{name}=${name}" in workflow_text
        assert f"secrets.{name}" not in workflow_text


def test_deploy_wires_safe_autonomy_variables():
    workflow_text = DEPLOY_WORKFLOW.read_text()
    defaults = {
        "META_AUTONOMY_ENABLED": "false",
        "META_AUTONOMY_SHADOW": "true",
        "META_AUTONOMY_CAMPAIGN_IDS_CSV": "",
        "META_AUTONOMY_MAX_REPLACEMENTS_24H": "1",
        "META_AUTONOMY_COOLDOWN_HOURS": "24",
        "META_AUTONOMY_MAX_TEST_DAYS": "7",
        "META_AUTONOMY_MAX_INCREASE_PERCENT": "20",
        "META_AUTONOMY_MAX_DAILY_BUDGET_EUR": "20",
    }

    for name, default in defaults.items():
        assert f"{name}: ${{{{ vars.{name} || '{default}' }}}}" in workflow_text
        assert f"{name}=${name}" in workflow_text
        assert f"secrets.{name}" not in workflow_text
