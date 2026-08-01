"""Configuration: a TOML file overridden by environment variables (ROADMAP M0).

Resolution order, highest priority first:

1. Explicit arguments to :func:`load_settings` (what the CLI passes for ``--cache-dir`` etc.)
2. ``INVESTO_*`` environment variables
3. ``.env`` in the working directory
4. The TOML config file
5. Field defaults declared below

Every environment variable carries the ``INVESTO_`` prefix, **including the LLM provider
keys**. Reading an ambient ``ANTHROPIC_API_KEY`` would give config resolution two conventions,
and would let a key inherited from a shell profile silently enable a paid code path that
``--llm none`` is supposed to be the only way out of.

One field has no default and cannot get one: ``sec_user_agent``. SEC requires a declared
User-Agent and rejects the library default (DESIGN.md §4.1). Inventing a fallback would send
this tool's traffic under a contact address that cannot answer for it, so an unset value is a
:class:`~investo.errors.ConfigError` — exit 5, before any network call.
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from investo.errors import ConfigError

__all__ = [
    "LLMProvider",
    "PriceProvider",
    "MIN_LOOKBACK_YEARS",
    "DEFAULT_LOOKBACK",
    "Settings",
    "load_settings",
    "parse_lookback",
    "default_config_paths",
]

LLMProvider = Literal["anthropic", "openai", "gemini", "none"]
PriceProvider = Literal["tiingo", "yfinance", "stooq"]

MIN_LOOKBACK_YEARS = 3
"""README § Usage: ``--lookback`` has a minimum of 3y.

Three years of annual data is four to five annual observations at best. The floor is not
"enough for a good estimate" — it is the point below which the trend model in DESIGN.md §5.2
has fewer observations than parameters and reports intervals it has not earned.
"""

DEFAULT_LOOKBACK = "5y"

_LOOKBACK_RE = re.compile(r"^(?P<n>\d+)y$")

# RFC 2606 reserves these so they can never resolve, which is precisely why .env.example uses
# one — and precisely why a live run must not. Rejecting the placeholder is the difference
# between SEC seeing a contact address and SEC seeing a shape where one should be.
_RESERVED_EMAIL_DOMAINS = ("example.com", "example.org", "example.net", "example.edu")

_toml_path: ContextVar[Path | None] = ContextVar("investo_toml_path", default=None)
"""The TOML file for the load in progress.

pydantic-settings builds its sources itself, inside ``settings_customise_sources``, which
receives no arguments from the caller — so a per-load file path has to reach it out of band.
The two obvious alternatives are both worse: ``Settings(_toml_file=...)`` is not part of
``BaseSettings.__init__``, so under ``extra="forbid"`` it is rejected as an unknown field, and
assigning ``Settings.model_config["toml_file"]`` leaks the path into every later load in the
process — including the next test.

A ``ContextVar`` rather than a module global so concurrent loads under threads or asyncio
cannot see each other's path. :func:`load_settings` resets it in a ``finally``.
"""


def default_config_paths() -> tuple[Path, ...]:
    """TOML config locations, in the order they are searched. First existing file wins.

    Project-local before user-global, so a checkout can pin its own settings without touching
    the machine, and a machine can hold a User-Agent that every checkout inherits.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    user_config = Path(xdg) if xdg else Path.home() / ".config"
    return (Path("investo.toml"), user_config / "investo" / "investo.toml")


def parse_lookback(value: str) -> int:
    """Parse a ``--lookback`` duration such as ``"5y"`` into a whole number of years.

    Only whole years are accepted. README § Usage documents ``5y`` and ``10y``; a quarters or
    months spelling would need a rule for what a fractional estimation window means to the
    annual series, and inventing one here would put that rule in two places once
    ``domain/periods.py`` lands in M1.

    Raises:
        ConfigError: if the spelling is unrecognised, or the window is under
            :data:`MIN_LOOKBACK_YEARS`.
    """
    match = _LOOKBACK_RE.match(value.strip().lower())
    if match is None:
        raise ConfigError(
            f"--lookback {value!r} is not a recognised duration.",
            hint="Use whole years, e.g. 5y or 10y.",
        )
    years = int(match.group("n"))
    if years < MIN_LOOKBACK_YEARS:
        raise ConfigError(
            f"--lookback {value!r} is below the {MIN_LOOKBACK_YEARS}y minimum.",
            hint=(
                f"{years} year(s) leaves too few annual observations to estimate a trend from. "
                f"Use {MIN_LOOKBACK_YEARS}y or more."
            ),
        )
    return years


class Settings(BaseSettings):
    """Resolved configuration for one run.

    Constructed through :func:`load_settings`, which turns pydantic's ``ValidationError`` into
    a :class:`~investo.errors.ConfigError`. Instantiating this class directly bypasses that
    translation and will surface a pydantic traceback instead of exit 5.
    """

    model_config = SettingsConfigDict(
        env_prefix="INVESTO_",
        env_file=".env",
        env_file_encoding="utf-8",
        # An unrecognised key is a typo, and a typo in a config file is silent by default: the
        # run proceeds with the intended setting unset. `forbid` converts that into exit 5 with
        # the offending key named.
        extra="forbid",
        frozen=True,
    )

    # --- Required -----------------------------------------------------------
    sec_user_agent: str = Field(
        description="Declared User-Agent for sec.gov, format `<app name> <contact email>`."
    )

    # --- Optional credentials ----------------------------------------------
    tiingo_key: str | None = None
    anthropic_key: str | None = None
    openai_key: str | None = None
    gemini_key: str | None = None

    # --- Paths --------------------------------------------------------------
    cache_dir: Path = Path(".cache")
    out_dir: Path = Path("reports")

    # --- Behaviour ----------------------------------------------------------
    price_provider: PriceProvider = "tiingo"
    llm_provider: LLMProvider = "none"
    """Default for ``--llm``. ``none`` per README: the LLM path is opt-in, and a complete
    report is producible without it."""

    lookback: str = DEFAULT_LOOKBACK

    # EDGAR's own cap is 10 req/s across all your machines, and exceeding it throttles the IP
    # until the rate stays under for ten minutes (DESIGN.md §4.1). 5 is half of that: the
    # penalty for being slightly too fast is minutes of downtime, and the reward for being
    # exactly at the limit is nothing.
    edgar_requests_per_second: float = Field(default=5.0, gt=0, le=10)

    coverage_floor: Decimal | None = Field(default=None, ge=0, le=1)
    """Tier-1 annual fill rate below which ``analyze`` exits 3. **Unset by default.** [M3]

    DESIGN.md §4.2 sanctions *a configurable floor* — coverage *"below a configurable floor degrades
    the report's confidence rating and can trigger an 'insufficient data' verdict"* — and supplies no
    number. `docs/m2/COVERAGE.md` is the measurement that would, and it does not exist yet.

    Defaulting to ``None`` rather than to a plausible-looking figure is the same call
    ``pyproject.toml``'s unset ``fail_under`` makes, for the same reason: a threshold invented before
    the measurement fires arbitrarily, and the first person it annoys tunes it rather than
    investigating. Resolve it the way that comment specifies — measure, set, and put the measured
    figure in the commit message.

    ``Decimal``, not ``float``, because it is compared against ``MetricCoverage.fill_rate``, and a
    mixed comparison is where a rate of exactly the floor stops being reproducible.
    """

    @field_validator("sec_user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        """Reject the values SEC will reject, and the placeholder from ``.env.example``.

        Checked here rather than at the first request because the failure is free to detect at
        startup and expensive to discover after a fetch has already begun — and because a
        throttled IP is not a per-run problem.
        """
        agent = value.strip()
        if not agent:
            raise ValueError("must not be empty")
        if "@" not in agent:
            raise ValueError(
                "must include a contact email address — SEC's documented format is "
                "`<app or company name> <contact email>`"
            )
        lowered = agent.lower()
        if any(domain in lowered for domain in _RESERVED_EMAIL_DOMAINS):
            raise ValueError(
                "still contains the example.com placeholder address. RFC 2606 reserves that "
                "domain so it can never receive mail; SEC needs an address that can. Use a "
                "real one"
            )
        return agent

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Insert the TOML source below env and ``.env``, per the module docstring's order.

        The path comes from the :data:`_toml_path` context variable; see its docstring for why
        it cannot simply be an argument. ``toml_file=None`` yields an empty source, which is
        what "no config file" should mean.
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=_toml_path.get()),
            file_secret_settings,
        )

    def api_key_for(self, provider: LLMProvider) -> str | None:
        """The configured key for ``provider``, or ``None`` for ``"none"``.

        Returns ``None`` rather than raising when a key is absent: whether a missing key is
        fatal depends on whether the caller asked for that provider, which is the caller's
        question to answer.
        """
        if provider == "none":
            return None
        return {
            "anthropic": self.anthropic_key,
            "openai": self.openai_key,
            "gemini": self.gemini_key,
        }[provider]


def _first_existing(paths: tuple[Path, ...]) -> Path | None:
    return next((p for p in paths if p.is_file()), None)


def load_settings(
    *,
    config_file: Path | None = None,
    cache_dir: Path | None = None,
    out_dir: Path | None = None,
    **overrides: object,
) -> Settings:
    """Resolve configuration, or raise :class:`~investo.errors.ConfigError`.

    Args:
        config_file: TOML file to read. When ``None``, the first existing path in
            :func:`default_config_paths` is used; when that finds nothing, only the
            environment and the declared defaults apply.
        cache_dir: Overrides ``cache_dir`` when not ``None`` (the CLI's ``--cache-dir``).
        out_dir: Overrides ``out_dir`` when not ``None`` (the CLI's ``--out``).
        **overrides: Further field overrides, highest precedence.

    ``None`` arguments are dropped rather than passed through, so an omitted CLI flag falls
    through to the environment instead of overriding it with a null.

    Raises:
        ConfigError: on any missing or invalid setting, with every pydantic complaint listed
            and the required-field case given its own message. Exit code 5.
    """
    toml_path = config_file if config_file is not None else _first_existing(default_config_paths())
    if config_file is not None and not config_file.is_file():
        raise ConfigError(
            f"Config file not found: {config_file}",
            hint="Pass a path that exists, or omit the flag to use the default search paths.",
        )

    # `Any`, not `object`: pyright checks an unpacked dict's value type against each synthesized
    # keyword parameter of `Settings.__init__`, and `object` is not assignable to `str` or `Path`.
    explicit: dict[str, Any] = {
        k: v
        for k, v in {"cache_dir": cache_dir, "out_dir": out_dir, **overrides}.items()
        if v is not None
    }

    token = _toml_path.set(toml_path)
    try:
        return Settings(**explicit)
    except ValidationError as exc:
        raise ConfigError(_describe(exc, toml_path), hint=_hint_for(exc)) from exc
    finally:
        _toml_path.reset(token)


def _describe(exc: ValidationError, toml_path: Path | None) -> str:
    """Render every pydantic complaint, naming the field both ways.

    Both spellings, not just the uppercased one: a bad value can arrive from the TOML file as
    easily as from the environment, and reporting `INVESTO_PRICE_PROVDIER` for a line that reads
    `price_provdier = "stooq"` sends the reader looking for a variable they never set.
    """
    lines = ["Configuration is invalid:"]
    for error in exc.errors():
        name = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {name} (env: INVESTO_{name.upper()}): {error['msg']}")
    source = f"config file: {toml_path}" if toml_path else "no config file found"
    lines.append(f"  ({source}; environment variables use the INVESTO_ prefix)")
    return "\n".join(lines)


def _hint_for(exc: ValidationError) -> str | None:
    """A hint only when there is an action to name — see :class:`InvestoError`."""
    missing = {
        ".".join(str(part) for part in error["loc"])
        for error in exc.errors()
        if error["type"] == "missing"
    }
    if "sec_user_agent" in missing:
        return (
            "SEC requires a declared User-Agent and there is deliberately no default. Set\n"
            '  export INVESTO_SEC_USER_AGENT="Investo research you@your-domain.com"\n'
            "See .env.example, or DESIGN.md §4.1 for why no default is provided."
        )
    if missing:
        return "Set the variable(s) above, or add them to investo.toml. See .env.example."
    return None
