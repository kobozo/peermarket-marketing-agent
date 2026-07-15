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
