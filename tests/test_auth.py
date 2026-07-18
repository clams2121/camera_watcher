import pytest

from camera_watcher.auth import (
    TOKEN_MIN_LENGTH,
    AuthConfigError,
    generate_token,
    require_token,
    tokens_match,
)


def test_generate_token_is_long_and_random():
    a, b = generate_token(), generate_token()
    assert len(a) >= TOKEN_MIN_LENGTH
    assert a != b


def test_require_token_returns_a_configured_token():
    assert require_token({"web": {"auth_token": "x" * TOKEN_MIN_LENGTH}}) == "x" * TOKEN_MIN_LENGTH


def test_require_token_fails_loud_when_missing():
    with pytest.raises(AuthConfigError, match="missing or too short"):
        require_token({"web": {}})


def test_require_token_fails_loud_when_blank():
    with pytest.raises(AuthConfigError):
        require_token({"web": {"auth_token": ""}})


def test_require_token_fails_loud_when_too_short():
    with pytest.raises(AuthConfigError):
        require_token({"web": {"auth_token": "short"}})


def test_require_token_fails_loud_when_web_section_absent():
    with pytest.raises(AuthConfigError):
        require_token({})


def test_tokens_match_true_for_identical_tokens():
    assert tokens_match("abc123", "abc123") is True


def test_tokens_match_false_for_different_tokens():
    assert tokens_match("abc123", "abc124") is False


def test_tokens_match_false_for_different_length_tokens():
    assert tokens_match("short", "a much longer token entirely") is False
