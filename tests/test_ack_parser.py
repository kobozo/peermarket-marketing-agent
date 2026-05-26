"""Slack ack parser — pure regex."""

from peermarket_agent.slack_bridge.ack_parser import parse_ack


def test_parse_approve_with_emoji():
    assert parse_ack("✅ 42") == ("approve", 42)


def test_parse_approve_no_space():
    assert parse_ack("✅42") == ("approve", 42)


def test_parse_reject_with_emoji():
    assert parse_ack("❌ 7") == ("reject", 7)


def test_parse_approve_colon_code():
    assert parse_ack(":white_check_mark: 42") == ("approve", 42)


def test_parse_reject_colon_code():
    assert parse_ack(":x: 7") == ("reject", 7)


def test_parse_colon_code_case_insensitive():
    assert parse_ack(":WHITE_CHECK_MARK: 99") == ("approve", 99)


def test_parse_in_middle_of_message():
    assert parse_ack("OK looks good ✅ 5 thanks") == ("approve", 5)


def test_parse_no_match_returns_none():
    assert parse_ack("hello") is None
    assert parse_ack("✅ but no number") is None
    assert parse_ack("42 alone") is None
    assert parse_ack("") is None


def test_parse_none_text_returns_none():
    assert parse_ack(None) is None  # type: ignore[arg-type]
