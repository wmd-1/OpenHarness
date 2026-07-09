"""Tests for the extra_oh_args allowlist validator."""

import pytest

from app.security import InvalidOhArgError, vet_extra_oh_args


def test_forbidden_permission_mode_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--permission-mode", "evil"])


def test_forbidden_output_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--output", "/evil"])


def test_forbidden_output_format_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--output-format", "json"])


def test_bogus_flag_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--bogus"])


def test_non_flag_token_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["foo"])


def test_flag_missing_value_rejected():
    with pytest.raises(InvalidOhArgError):
        vet_extra_oh_args(["--temperature"])


def test_allowed_flag_with_value_ok():
    assert vet_extra_oh_args(["--temperature", "0.7"]) == ["--temperature", "0.7"]


def test_allowed_flag_without_value_ok():
    assert vet_extra_oh_args(["--no-cache"]) == ["--no-cache"]


def test_empty_input_ok():
    assert vet_extra_oh_args([]) == []
    assert vet_extra_oh_args(None) == []


def test_mixed_allowed_flags_ok():
    assert vet_extra_oh_args(["--model", "gpt-4", "--verbose"]) == [
        "--model",
        "gpt-4",
        "--verbose",
    ]
