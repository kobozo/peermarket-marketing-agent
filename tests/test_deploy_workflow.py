"""Deployment workflow contracts that protect runtime Slack configuration."""

import os
import re
import subprocess
from pathlib import Path

import yaml

DEPLOY_WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml"
RUNBOOK = Path(__file__).parents[1] / "docs" / "autonomous-ad-lifecycle-runbook.md"
HOOK_RUNBOOK = Path(__file__).parents[1] / "docs" / "multitest-hook-experiment-runbook.md"


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
        "META_AUTONOMY_EXPERIMENT_ID": "",
        "META_AUTONOMY_VARIANT_COUNT": "3",
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


def test_hook_experiment_runbook_is_ci_only_shadow_first_and_has_kill_switch():
    text = HOOK_RUNBOOK.read_text()
    for required in (
        "Draft 156",
        "120249125021520342",
        "META_AUTONOMY_EXPERIMENT_ID",
        "META_AUTONOMY_VARIANT_COUNT --body 3",
        "META_AUTONOMY_SHADOW --body true",
        "peermarket-performance autonomy --draft-id 156",
        "gh workflow run deploy.yml",
        "kill switch",
    ):
        assert required.casefold() in text.casefold()
    assert "gh secret set" not in text
    assert "peermarket-performance prepare-hook-experiment --draft-id 156" in text
    assert (
        "all three ordered variant IDs"
        in (Path(__file__).parents[1] / "docs" / "autonomous-ad-lifecycle-runbook.md").read_text()
    )
    autonomy_text = " ".join(RUNBOOK.read_text().split())
    for audit_field in (
        "configured experiment ID",
        "all three ordered variant IDs",
        "thresholds",
        "per-variant samples",
        "complete fresh evidence window",
        "next evaluation",
        "neutral",
        "shadow",
    ):
        assert audit_field.casefold() in autonomy_text.casefold()
    assert (
        "peermarket-performance prepare-hook-experiment --draft-id 156"
        in DEPLOY_WORKFLOW.read_text()
    )


def test_hook_canary_uses_correlated_ci_dispatch_and_deployed_read_only_gate():
    runbook = HOOK_RUNBOOK.read_text()
    workflow = DEPLOY_WORKFLOW.read_text()
    for line in (
        "gh variable set META_INSIGHTS_ENABLED --body true",
        "gh variable set PEERMARKET_ATTRIBUTION_ENABLED --body true",
        "gh variable set META_AUTONOMY_ENABLED --body true",
        "gh variable set META_AUTONOMY_SHADOW --body true",
        "gh variable set META_AUTONOMY_CAMPAIGN_IDS_CSV --body 120249125021520342",
        "gh variable set META_AUTONOMY_MAX_INCREASE_PERCENT --body 20",
        "gh variable set META_AUTONOMY_MAX_DAILY_BUDGET_EUR --body 20",
        "gh variable set META_AUTONOMY_MAX_REPLACEMENTS_24H --body 1",
        "gh variable set META_AUTONOMY_MAX_TEST_DAYS --body 7",
        "gh variable set META_AUTONOMY_COOLDOWN_HOURS --body 24",
    ):
        assert line in runbook
    assert '--commit "$head_sha" --created ">=$boundary"' in runbook
    assert 'gh run watch "$run_id" --exit-status' in runbook
    assert '[[ -n "$candidate" ]] && run_ids+=("$candidate")' in runbook
    assert "exactly `:01`, `:02`, and `:03`" in runbook
    assert "fixed landing page and audience identity" in runbook
    assert "policy-recognized neutral or qualified evidence" in runbook
    assert "no queued action" in runbook
    assert "durable Slack audit" in runbook
    assert "explicit later canary approval" in runbook
    assert "Verify hook experiment shadow canary" in workflow
    assert "peermarket-performance autonomy --draft-id 156" in workflow
    assert "EXPECTED_EXPERIMENT_ID" in workflow
    assert 'report["active_action_count"] == 0' in workflow
    assert '{"pending", "delivered"}' in workflow
    assert 'evidence["variant_ids"] == expected' in workflow
    assert 'audit["decision_id"] == report["decision"]["id"]' in workflow
    assert 'audit["experiment_id"] == experiment_id' in workflow
    assert 'audit["evidence_window"] == evidence["window"]' in workflow
    assert 'limits["max_daily_budget_cents"]' in workflow


def test_hook_dispatch_retries_blank_run_list_before_unique_numeric_id(tmp_path):
    runbook = HOOK_RUNBOOK.read_text()
    function = re.search(
        r"dispatch_hook_canary\(\) \{.*?^\}", runbook, re.MULTILINE | re.DOTALL
    ).group(0)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    counter = tmp_path / "counter"
    watched = tmp_path / "watched"
    (bin_dir / "git").write_text(
        '#!/bin/sh\n[ "$1" = branch ] && echo feature || echo 0123456789012345678901234567890123456789\n'
    )
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
    (bin_dir / "gh").write_text(
        "#!/bin/sh\n"
        "if [ \"$1 $2\" = 'workflow run' ]; then exit 0; fi\n"
        f"if [ \"$1 $2\" = 'run list' ]; then n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}; [ $n -lt 3 ] || echo 4242; exit 0; fi\n"
        f'if [ "$1 $2" = \'run watch\' ]; then echo "$3 $4" > {watched}; exit 0; fi\n'
        "exit 1\n"
    )
    for script in bin_dir.iterdir():
        script.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", f"{function}\ndispatch_hook_canary"],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert counter.read_text().strip() == "3"
    assert watched.read_text().strip() == "4242 --exit-status"


def test_hook_verifier_uses_project_venv_and_src_layout_import():
    workflow = DEPLOY_WORKFLOW.read_text()
    assert "cd /opt/peermarket-agent" in workflow
    assert "REPORT=\"$report\" /opt/peermarket-agent/.venv/bin/python - <<'PY'" in workflow
    verifier = workflow.split("- name: Verify hook experiment shadow canary", 1)[1]
    assert 'REPORT="$report" python3' not in verifier

    root = DEPLOY_WORKFLOW.parents[2]
    result = subprocess.run(
        [
            str(root / ".venv" / "bin" / "python"),
            "-c",
            "from peermarket_agent.cli_performance import classify_experiment_reason; "
            "assert classify_experiment_reason('insufficient_evidence') == 'neutral'",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_autonomy_runbook_has_exact_ci_only_canary_controls_without_credentials():
    text = RUNBOOK.read_text()
    required = {
        "gh variable set META_INSIGHTS_ENABLED --body true",
        "gh variable set PEERMARKET_ATTRIBUTION_ENABLED --body true",
        "gh variable set META_AUTONOMY_ENABLED --body false",
        "gh variable set META_AUTONOMY_SHADOW --body true",
        "gh variable set META_AUTONOMY_CAMPAIGN_IDS_CSV --body 120249125021520342",
        "gh variable set META_AUTONOMY_MAX_INCREASE_PERCENT --body 20",
        "gh variable set META_AUTONOMY_MAX_DAILY_BUDGET_EUR --body 20",
        "gh variable set META_AUTONOMY_MAX_REPLACEMENTS_24H --body 1",
        "gh variable set META_AUTONOMY_MAX_TEST_DAYS --body 7",
        "gh variable set META_AUTONOMY_COOLDOWN_HOURS --body 24",
        "peermarket-performance autonomy --draft-id 156",
        "curl -fsS http://127.0.0.1:8090/agent/healthz",
    }
    assert required <= set(text.splitlines())
    assert 'gh workflow run deploy.yml --ref "$deploy_ref"' in text
    assert (
        'gh run list --workflow deploy.yml --event workflow_dispatch --branch "$deploy_ref" '
        '--commit "$head_sha" --created ">=$boundary" --limit 20 --json databaseId '
        '--jq ".[].databaseId"'
    ) in text
    assert 'gh run watch "$run_id" --exit-status' in text
    assert "Draft 156" in text
    assert "reconciliation_required" in text
    assert "kill switch" in text.casefold()
    assert "verified audit result" not in text.casefold()
    assert "rollback_recorded" in text
    assert "failure_category" in text
    assert "dispatch_deploy()" in text
    assert text.splitlines().count("dispatch_deploy") == 4
    assert '[[ ! "$run_id" =~ ^[0-9]+$ ]]' in text
    assert "${#run_ids[@]} > 1" in text
    assert "for attempt in {1..12}" in text
    assert "run_url=" not in text
    assert "gh run watch\n" not in text
    assert "gh secret set" not in text
    for forbidden in ("sk-", "xoxb-", "Bearer ", "password=", "token="):
        assert forbidden not in text
