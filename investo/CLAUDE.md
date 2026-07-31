# CLAUDE.md

Guidance for Claude Code (and other AI assistants) contributing to **investo**.

## What this project is

Investo turns a NASDAQ ticker into a fundamental due-diligence PDF built from SEC filings. It
is currently at **M0** — the CLI shell, the config layer and the exit-code taxonomy. No data is
fetched, normalized, analyzed or rendered yet.

`DESIGN.md` is **normative** on architecture and data handling; `ROADMAP.md` is normative on
sequencing and scope. On any conflict between those documents and a comment or docstring in the
code, the documents govern — raise the discrepancy, do not resolve it silently in code.

## The two properties everything must preserve

Stated in README and DESIGN §3.2, and repeated here because every future milestone can break
them:

1. **Every number traces to a source.** Each figure carries the accession number, XBRL tag and
   fetch timestamp it came from. If it cannot be traced, it is not printed.
2. **The LLM cannot touch the numbers.** All figures come from deterministic math. The LLM's
   output schema has no numeric field feeding anything downstream — including the verdict, the
   bull/bear case and the confidence rating, all of which are composed from computed flags and
   score components rather than generated.

## Current layout

```
src/investo/
├── __init__.py       # package docstring; re-exports the three modules
├── __main__.py       # `python -m investo`
├── cli.py            # typer app — every documented command and flag, stub bodies
├── config.py         # pydantic-settings: TOML + env, INVESTO_ prefix
└── errors.py         # ExitCode (DESIGN §14) + the exception hierarchy
tests/                # pytest — config resolution, exit codes, CLI surface
```

DESIGN §3.1 shows the full module tree. **It is created per milestone, not up front.** An empty
package cannot be type-checked or tested, and goes stale before it is filled. M1 adds
`domain/` and `ingest/`, M2 `normalize/`, and so on.

Two places where the code deliberately differs from the documents, both recorded in
ROADMAP § Decided during design:

- **`src/` layout**, where DESIGN §3.1 draws a flat `investo/`. Prevents tests importing the
  working tree instead of the installed package.
- **BasedPyright**, where ROADMAP M0 said `mypy --strict`. Same type checker as the sibling
  `tradipy` repo — though not the same setting: tradipy runs `standard`, investo runs `strict`,
  because M0 asks for strict and a greenfield codebase can afford what a retrofit cannot.

## Non-negotiable conventions

1. **Exit codes live in one place.** `errors.ExitCode`, matching DESIGN §14. A new error class
   declares its own `exit_code`; `tests/test_errors.py` walks the hierarchy and fails if one
   does not. Inheriting the base default silently reports an upstream fetch failure that never
   happened.
2. **The SEC User-Agent has no default and never gets one** (DESIGN §4.1). Unset is exit 5,
   before any network call. Do not add a fallback, and do not let a test pass one implicitly —
   `tests/conftest.py` clears the whole `INVESTO_*` environment for exactly that reason.
3. **Every `INVESTO_` variable, including LLM keys.** Never read an ambient `ANTHROPIC_API_KEY`.
   One convention for config resolution, and no way for an inherited shell variable to enable
   a paid code path that `--llm none` is supposed to be the only exit from.
4. **A CLI flag that mirrors a config field defaults to `None`.** A typer default is
   indistinguishable from a typed value, so `--lookback` defaulting to `"5y"` would outrank the
   config file on every run and make that setting dead. `None` means "not specified" and
   `load_settings` drops it.
5. **README § Usage is the CLI's specification.** `tests/test_cli_surface.py` checks it in both
   directions: a documented flag that does not exist fails, and an accepted flag that is not
   documented fails too. Adding a flag means adding a README line and a `_FLAG_OWNER` entry.
6. **Nothing outside `ingest/edgar/client.py` may call sec.gov** (from M1). Single choke point,
   token bucket at 5 req/s against SEC's cap of 10, mandatory User-Agent. The penalty for being
   slightly too fast is a throttled IP for ten minutes, and it is not only your traffic.
7. **CI sets no `INVESTO_*` variables.** A test that reaches the network should fail rather
   than quietly succeed. Keep it that way when M1 adds an HTTP client — use recorded fixtures.
8. **`Decimal` for money, never `float`.** Applies from M1, when the first financial figure is
   parsed.
9. **Determinism is a feature, and it is configured up front** (M3). `SOURCE_DATE_EPOCH`,
   pinned `svg.hashsalt`, `metadata={"Date": None}`, and the LLM response cache keyed on
   `(prompt version, document hash, model id)` so the LLM path is inside the determinism gate
   rather than exempt from it. Two runs must produce a byte-identical PDF.

## Coding standards

- Python 3.13. Modern typing (`X | None`, builtin generics, `collections.abc`), `pathlib`,
  `dataclasses`, `enum`. No legacy `typing` aliases.
- Small functions, explicit types on public APIs, Google-style docstrings on public modules,
  classes and functions.
- Formatting and imports are Ruff's job. Never hand-format.
- typer options use the `Annotated[T, typer.Option(...)]` form, not a `typer.Option(...)`
  default — so flake8-bugbear's B008 stays enabled everywhere.
- Dependencies arrive with the milestone that imports them. Do not add one early.

## Testing expectations

- Every behavior change needs a test. Use the `spec` marker for a rule stated normatively in
  DESIGN.md or ROADMAP.md, and `surface` for the CLI's documented flag surface.
- **Assert the derivation, not the value.** A test that hard-codes an expected number passes
  under a wrong rule that happens to agree at that input.
- **For any sentence of the form "X cannot happen", write the test that attempts X and asserts
  it fails.** A happy-path test passes whether or not the guarantee is enforced.
- Boundaries get their own test. "Minimum 3y" needs 3y asserted as *accepted*, or a `>` where
  `>=` belongs survives every test that only probes 1y and 5y.
- `make check` must be green before work is done.

## Dependency management

Use `uv` exclusively: `uv sync`, `uv run ...`, `uv add ...`. Dev tools live in the `dev`
dependency group. Commit changes to `uv.lock`.

## Documentation requirements

When behavior changes: update `README.md` if the CLI surface moved, `DESIGN.md` if the
architecture did, `ROADMAP.md` if scope or a decision did, and any affected docstring. A rule
in the code that diverges from DESIGN.md is a spec question — raise it.

## Review checklist

- [ ] Exit code comes from `ExitCode`, and any new error class declares its own.
- [ ] No default added for `sec_user_agent`; no vendor-prefixed env var read.
- [ ] New CLI flag documented in README § Usage and entered in `_FLAG_OWNER`.
- [ ] A flag mirroring a config field defaults to `None`.
- [ ] Tests added, with the right marker, asserting derivations and boundaries.
- [ ] Every new guarantee has a test that performs the violation it forbids.
- [ ] No new dependency without the milestone that needs it.
- [ ] `make check` passes.
