"""Shared fixtures.

The whole suite runs with the ``INVESTO_*`` environment cleared. Without that, a developer who
exports ``INVESTO_SEC_USER_AGENT`` in their shell — which everyone working on this will — makes
``test_missing_user_agent_is_config_error`` pass for the wrong reason locally and fail in CI,
or worse, the reverse. Autouse, so a test cannot forget.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

VALID_USER_AGENT = "Investo test suite tests@investo.invalid"
"""A User-Agent that passes validation. ``.invalid`` is RFC 2606-reserved and, unlike
``example.com``, is not on the rejected-placeholder list — so it exercises the accept path
without being an address anyone might mistake for real."""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Remove every ``INVESTO_*`` variable and run from an empty directory.

    ``chdir`` matters as much as the variable clearing: ``Settings`` reads ``.env`` and
    ``investo.toml`` relative to the working directory, so a suite run from the repo root would
    pick up whatever the developer has there.
    """
    for key in [k for k in os.environ if k.startswith("INVESTO_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # typer renders help through rich, which soft-wraps to the terminal width. At 80 columns a
    # long flag can break mid-token, so `test_no_undocumented_flags` would be measuring the
    # terminal rather than the CLI. NO_COLOR keeps ANSI escapes out of the matched text.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.chdir(tmp_path)
    yield


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set the one required variable, so a test can exercise something past config."""
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
