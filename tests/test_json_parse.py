"""parse_claude_json — fence-stripping JSON parser tests."""

import pytest

from peermarket_agent._json_parse import parse_claude_json


def test_plain_json():
    assert parse_claude_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_json_with_json_fence():
    raw = '```json\n{"a": 1}\n```'
    assert parse_claude_json(raw) == {"a": 1}


def test_json_with_uppercase_json_fence():
    raw = '```JSON\n{"a": 1}\n```'
    assert parse_claude_json(raw) == {"a": 1}


def test_json_with_bare_fence():
    raw = '```\n{"a": 1}\n```'
    assert parse_claude_json(raw) == {"a": 1}


def test_json_with_leading_and_trailing_whitespace():
    raw = '   \n```json\n{"a": 1}\n```\n   '
    assert parse_claude_json(raw) == {"a": 1}


def test_multiline_json_inside_fence():
    raw = '```json\n{\n  "hook": "Marktplaats moe?",\n  "body": "Verkoop veilig.",\n  "cta": "Plaats nu"\n}\n```'
    parsed = parse_claude_json(raw)
    assert parsed["hook"] == "Marktplaats moe?"
    assert parsed["cta"] == "Plaats nu"


def test_invalid_json_raises_valueerror():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_claude_json("not json at all")


def test_invalid_json_inside_fence_raises_valueerror():
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_claude_json("```json\nnot json inside\n```")


def test_truncates_long_text_in_error():
    long_garbage = "x" * 500
    with pytest.raises(ValueError) as exc_info:
        parse_claude_json(long_garbage)
    # Error message should NOT contain all 500 chars
    assert len(str(exc_info.value)) < 400
