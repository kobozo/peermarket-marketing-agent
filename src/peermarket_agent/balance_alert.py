"""Anthropic credit-balance-low detection + topup nudge content."""

from anthropic import BadRequestError

NUDGE_MESSAGE = (
    ":warning: *PeerMarket agent paused — Anthropic credits exhausted*\n\n"
    "I tried to generate a draft but got `400 invalid_request_error: "
    "Your credit balance is too low`.\n\n"
    "*To resume:*\n"
    "1. Top up at https://console.anthropic.com/settings/billing (~€30 recommended)\n"
    "2. Set workspace soft limit to €100 if not already (Plans & Billing → Limits)\n"
    "3. Re-trigger any pending drafts manually — the hourly loop will recover on its own\n\n"
    "I'll keep heartbeating but won't generate new content until the balance is restored."
)


def is_credit_balance_error(exc: BaseException) -> bool:
    """Return True iff this exception is an Anthropic credit-low 400."""
    if not isinstance(exc, BadRequestError):
        return False
    msg = str(exc).lower()
    return "credit balance is too low" in msg
