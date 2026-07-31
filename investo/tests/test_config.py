"""Config layer: resolution order, the required User-Agent, and lookback parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from investo.config import (
    DEFAULT_LOOKBACK,
    MIN_LOOKBACK_YEARS,
    LLMProvider,
    default_config_paths,
    load_settings,
    parse_lookback,
)
from investo.errors import ConfigError, ExitCode
from tests.conftest import VALID_USER_AGENT


def _toml(tmp_path: Path, body: str) -> Path:
    """Write a config file somewhere `default_config_paths()` will *not* find it.

    Not `tmp_path/investo.toml`: `clean_env` chdirs into `tmp_path`, and the first default search
    path is `./investo.toml` — so a fixture written there is picked up by loads that are meant to
    see no config file at all, and `test_toml_path_does_not_leak_between_loads` would pass
    whether or not the leak it names exists.
    """
    path = tmp_path / "fixtures" / "investo.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _assign(target: object, name: str, value: object) -> None:
    """Set an attribute without the type checker or the linter objecting.

    A literal `settings.lookback = ...` is the clearer spelling, but whether pyright rejects it
    on a frozen pydantic model depends on the installed stubs, and a `# pyright: ignore` that
    stops being necessary becomes its own lint failure. Going through `setattr` with a variable
    name sidesteps both, and ruff's B010 only fires on a constant name.
    """
    setattr(target, name, value)


# ---------------------------------------------------------------------------
# The required User-Agent
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_missing_user_agent_is_config_error() -> None:
    """DESIGN.md §4.1: no default, and startup fails without one."""
    with pytest.raises(ConfigError) as caught:
        load_settings()
    assert caught.value.exit_code == ExitCode.CONFIG_ERROR
    assert "INVESTO_SEC_USER_AGENT" in caught.value.message


@pytest.mark.spec
def test_missing_user_agent_hint_names_the_variable_and_a_command() -> None:
    """The hint has to be actionable, per `InvestoError`'s contract.

    Asserted because a config error at startup is the first thing a new user will see, and
    "field required" alone does not tell them SEC requires this or how to set it.
    """
    with pytest.raises(ConfigError) as caught:
        load_settings()
    hint = caught.value.hint
    assert hint is not None
    assert "INVESTO_SEC_USER_AGENT" in hint
    assert "export" in hint


def test_user_agent_without_email_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", "Investo")
    with pytest.raises(ConfigError, match="contact email"):
        load_settings()


def test_user_agent_placeholder_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The literal value shipped in `.env.example` must not reach SEC.

    Not a hypothetical: copying `.env.example` to `.env` and running is the documented first
    step, so the placeholder is the single most likely wrong value.
    """
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", "Investo research your.email@example.com")
    with pytest.raises(ConfigError, match="placeholder"):
        load_settings()


def test_env_example_ships_a_value_this_validator_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`.env.example` and the validator must stay in step.

    If someone "fixes" `.env.example` to a value that passes, the placeholder guard above stops
    protecting anything real. This test fails when that happens, which is the only way to
    notice. It reads the file rather than repeating the string, so there is one copy.
    """
    example = Path(__file__).parent.parent / ".env.example"
    line = next(
        line
        for line in example.read_text(encoding="utf-8").splitlines()
        if line.startswith("INVESTO_SEC_USER_AGENT=")
    )
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", line.split("=", 1)[1].strip().strip('"'))
    with pytest.raises(ConfigError):
        load_settings()


def test_user_agent_is_accepted_and_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", f"  {VALID_USER_AGENT}  ")
    assert load_settings().sec_user_agent == VALID_USER_AGENT


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ROADMAP M0: "TOML file + env override" — env wins."""
    config = _toml(
        tmp_path,
        f'sec_user_agent = "{VALID_USER_AGENT}"\nprice_provider = "stooq"\n',
    )
    monkeypatch.setenv("INVESTO_PRICE_PROVIDER", "yfinance")
    assert load_settings(config_file=config).price_provider == "yfinance"


def test_toml_overrides_defaults(tmp_path: Path) -> None:
    config = _toml(tmp_path, f'sec_user_agent = "{VALID_USER_AGENT}"\nlookback = "10y"\n')
    settings = load_settings(config_file=config)
    assert settings.lookback == "10y"
    assert settings.price_provider == "tiingo", "unset field should keep its declared default"


def test_explicit_argument_overrides_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI flag beats the environment — the top of the resolution order."""
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_OUT_DIR", "/env/reports")
    assert load_settings(out_dir=tmp_path / "flag").out_dir == tmp_path / "flag"


def test_omitted_argument_does_not_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `None` argument falls through instead of nulling the environment's value.

    This is the bug the `if v is not None` filter in `load_settings` exists to prevent: typer
    passes `None` for every flag the user did not type, and passing those through would make
    every CLI invocation silently reset config to defaults.
    """
    monkeypatch.setenv("INVESTO_SEC_USER_AGENT", VALID_USER_AGENT)
    monkeypatch.setenv("INVESTO_CACHE_DIR", "/env/cache")
    assert load_settings(cache_dir=None).cache_dir == Path("/env/cache")


def test_defaults_when_nothing_is_configured(configured: None) -> None:
    settings = load_settings()
    assert settings.cache_dir == Path(".cache")
    assert settings.out_dir == Path("reports")
    assert settings.price_provider == "tiingo"
    assert settings.llm_provider == "none", "README: the LLM path is opt-in"
    assert settings.lookback == DEFAULT_LOOKBACK


def test_default_config_paths_prefer_project_over_user(tmp_path: Path) -> None:
    paths = default_config_paths()
    assert paths[0] == Path("investo.toml")
    assert paths[1].is_relative_to(tmp_path), "XDG_CONFIG_HOME should be honoured"


def test_project_local_config_is_discovered_without_a_flag(tmp_path: Path) -> None:
    """`./investo.toml` is read with no `--config`.

    The other TOML tests pass an explicit path, so without this one the whole default-search
    branch of `load_settings` would be untested — and a discovery bug would only show up for
    someone running the real CLI.
    """
    (tmp_path / "investo.toml").write_text(
        f'sec_user_agent = "{VALID_USER_AGENT}"\nlookback = "10y"\n', encoding="utf-8"
    )
    assert load_settings().lookback == "10y"


# ---------------------------------------------------------------------------
# Config file handling
# ---------------------------------------------------------------------------
def test_named_config_file_must_exist(tmp_path: Path, configured: None) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(config_file=tmp_path / "absent.toml")


def test_missing_default_config_file_is_not_an_error(configured: None) -> None:
    """No config file at all is a supported way to run — env alone is enough."""
    assert load_settings().sec_user_agent == VALID_USER_AGENT


def test_unknown_toml_key_is_rejected(tmp_path: Path) -> None:
    """`extra="forbid"`: a typo must not resolve to the default in silence."""
    config = _toml(tmp_path, f'sec_user_agent = "{VALID_USER_AGENT}"\nprice_provdier = "stooq"\n')
    with pytest.raises(ConfigError, match="price_provdier"):
        load_settings(config_file=config)


def test_toml_path_does_not_leak_between_loads(tmp_path: Path, configured: None) -> None:
    """Passing `_toml_file` per instantiation must not mutate class-level config.

    If it did, the second load below would still read the first file — the failure mode that
    makes tests pass in isolation and fail in sequence.
    """
    config = _toml(tmp_path, f'sec_user_agent = "{VALID_USER_AGENT}"\nlookback = "10y"\n')
    assert load_settings(config_file=config).lookback == "10y"
    assert load_settings().lookback == DEFAULT_LOOKBACK


def test_settings_are_frozen(configured: None) -> None:
    """Config is resolved once per run and then read-only.

    Matters more than it looks: a mutable `Settings` invites a code path that "just adjusts"
    the cache directory or the rate limit mid-run, and DESIGN.md's reproducibility claim rests
    on the run's configuration being one fixed thing that `report.json` can record.
    """
    settings = load_settings()
    with pytest.raises(ValidationError):
        _assign(settings, "lookback", "10y")


# ---------------------------------------------------------------------------
# Rate limit
# ---------------------------------------------------------------------------
@pytest.mark.spec
def test_edgar_rate_default_is_half_the_sec_cap(configured: None) -> None:
    """DESIGN.md §4.1: SEC caps at 10 req/s; investo runs at 5."""
    assert load_settings().edgar_requests_per_second == 5.0


@pytest.mark.spec
@pytest.mark.parametrize("rate", ["10.1", "0", "-1"])
def test_edgar_rate_above_the_sec_cap_is_rejected(
    rate: str, monkeypatch: pytest.MonkeyPatch, configured: None
) -> None:
    """The ceiling is enforced, not advisory. Exceeding 10 req/s throttles the IP for ten
    minutes, which is not a per-run problem — it affects every other user of that address."""
    monkeypatch.setenv("INVESTO_EDGAR_REQUESTS_PER_SECOND", rate)
    with pytest.raises(ConfigError):
        load_settings()


# ---------------------------------------------------------------------------
# api_key_for
# ---------------------------------------------------------------------------
def test_api_key_for_none_is_none(monkeypatch: pytest.MonkeyPatch, configured: None) -> None:
    """`--llm none` reads no key, even when one is configured."""
    monkeypatch.setenv("INVESTO_ANTHROPIC_KEY", "sk-test")
    assert load_settings().api_key_for("none") is None


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gemini"])
def test_api_key_for_reads_the_prefixed_variable(
    provider: LLMProvider, monkeypatch: pytest.MonkeyPatch, configured: None
) -> None:
    """Every provider key uses the INVESTO_ prefix, not the vendor's own variable name."""
    monkeypatch.setenv(f"INVESTO_{provider.upper()}_KEY", f"key-{provider}")
    settings = load_settings()
    assert settings.api_key_for(provider) == f"key-{provider}"


def test_vendor_env_var_is_not_read(monkeypatch: pytest.MonkeyPatch, configured: None) -> None:
    """An ambient ANTHROPIC_API_KEY must not enable the paid path.

    The whole reason for the INVESTO_ prefix on LLM keys: a key inherited from a shell profile
    should not be able to turn on a code path the user did not ask for.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
    assert load_settings().api_key_for("anthropic") is None


# ---------------------------------------------------------------------------
# parse_lookback
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "years"), [("3y", 3), ("5y", 5), ("10y", 10), (" 10Y ", 10)])
def test_parse_lookback_accepts_whole_years(text: str, years: int) -> None:
    assert parse_lookback(text) == years


@pytest.mark.spec
def test_parse_lookback_enforces_the_documented_minimum() -> None:
    """README § Usage: minimum 3y."""
    with pytest.raises(ConfigError, match=f"{MIN_LOOKBACK_YEARS}y minimum"):
        parse_lookback(f"{MIN_LOOKBACK_YEARS - 1}y")


@pytest.mark.spec
def test_parse_lookback_accepts_the_minimum_itself() -> None:
    """The boundary is inclusive — "minimum 3y" means 3y is legal.

    Written because the off-by-one here is invisible: a `>` where `>=` belongs still passes
    every test that only probes 1y and 5y.
    """
    assert parse_lookback(f"{MIN_LOOKBACK_YEARS}y") == MIN_LOOKBACK_YEARS


@pytest.mark.parametrize("text", ["5", "5years", "20q", "5.5y", "", "y", "-5y", "5m"])
def test_parse_lookback_rejects_other_spellings(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_lookback(text)


def test_default_lookback_is_valid() -> None:
    """The shipped default has to satisfy the shipped constraint."""
    assert parse_lookback(DEFAULT_LOOKBACK) >= MIN_LOOKBACK_YEARS
