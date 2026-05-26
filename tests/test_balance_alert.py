"""Credit-balance-error detection tests."""

import httpx
from anthropic import BadRequestError

from peermarket_agent.balance_alert import NUDGE_MESSAGE, is_credit_balance_error


def _make_anthropic_error(message: str) -> BadRequestError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(
        status_code=400,
        request=request,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
    )
    return BadRequestError(
        message=message,
        response=response,
        body={"error": {"message": message}},
    )


def test_is_credit_balance_error_true_for_balance_message():
    exc = _make_anthropic_error("Your credit balance is too low to access the Anthropic API.")
    assert is_credit_balance_error(exc) is True


def test_is_credit_balance_error_false_for_other_400():
    exc = _make_anthropic_error("Invalid temperature value")
    assert is_credit_balance_error(exc) is False


def test_is_credit_balance_error_false_for_non_anthropic_error():
    assert is_credit_balance_error(ValueError("anything")) is False
    assert is_credit_balance_error(RuntimeError("credit balance is too low")) is False


def test_nudge_message_includes_billing_url():
    assert "console.anthropic.com/settings/billing" in NUDGE_MESSAGE
