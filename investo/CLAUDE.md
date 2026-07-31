# CLAUDE.md

Guidance for Claude Code (and other AI assistants) contributing to **investo**.

## What this project is

Investo turns a NASDAQ ticker into a fundamental due-diligence PDF built from SEC filings. It
is currently at **M2 built**. `investo fetch TICKER`, `investo facts TICKER` and
`investo cache prune` work: EDGAR and price payloads are fetched through a rate-limited choke
point, cached immutably, parsed into typed rows keyed by XBRL tag, and normalized into per-metric
annual and quarterly series in which every figure traces to an accession and a tag. `facts --json`
emits DESIGN §4.5's `report.json`, which is under the §11 determinism gate. Nothing is analyzed or
rendered yet — `analyze` and `backtest` parse their full flag surface and exit 70 naming the
milestone that fills them in.

M2's design is in [`docs/m2/`](docs/m2/README.md) and its seventeen spec questions were accepted on
review; the decisions are in DESIGN §3.2, §4.2, §4.2.1, §4.5, §11 and ROADMAP § Decided during
design. Read `docs/m2/README.md` before touching `normalize/`.

**Two M2 workstreams are research and are not done**, and both are recorded rather than assumed:
the curated real-filing fixtures (so DESIGN §11's *"assert exact expected series"* is still
self-referential — see `tests/fixtures/edgar/PROVENANCE.md`) and the twenty-name coverage
measurement that `docs/m2/COVERAGE.md` is the record for. Until the second lands, the **tier-2 chain
orderings in `normalize/tags.py` are provisional** and DESIGN §4.2 deliberately does not carry them.

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
├── cli.py            # typer app — every documented command and flag        [M0]
├── config.py         # pydantic-settings: TOML + env, INVESTO_ prefix       [M0]
├── errors.py         # ExitCode (DESIGN §14) + the exception hierarchy      [M0]
├── fetch.py          # `investo fetch` orchestration, summary, absences     [M1]
├── facts.py          # `investo facts` orchestration, table, --json         [M2]
├── domain/           # models, periods, provenance — frozen, zero I/O       [M1]
├── ingest/
│   ├── cache.py      # content-addressed, append-only, manifest hash        [M1]
│   ├── edgar/        # client (the only sec.gov caller) + 9 parsers         [M1]
│   ├── finra.py      # short interest, snapshotted                          [M1]
│   └── prices/       # protocol + tiingo / yfinance / stooq                 [M1]
├── normalize/
│   ├── tags.py       # the chain registry — the only home for a us-gaap tag [M2]
│   ├── facts.py      # as_of, dedup, buckets, residual recovery             [M2]
│   └── statements.py # FinancialHistory, the period spine, coverage         [M2]
└── report/
    └── serialize.py  # report.json; charts and render arrive in M3          [M2]
tests/                # pytest — ~40 modules, fixtures, AST layering rules
```

DESIGN §3.1 shows the full module tree. **It is created per milestone, not up front.** An empty
package cannot be type-checked or tested, and goes stale before it is filled. M1 added `domain/`
and `ingest/`, M2 added `normalize/` and `report/`, M3 adds `report/`'s charts and templates, and
so on. `report/` holding one module after M2 is ROADMAP M2's stated intent, not an omission.

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
   parsed. From M2 an AST test forbids constructing a `float` anywhere under `normalize/` or
   `report/`, and `report.json` emits values as **JSON strings**: a JSON number is an IEEE double
   to most parsers, and a round-trip test with `parse_float=Decimal` passes either way, so the
   assertion is on the quoting in the serialized bytes.
9. **`normalize/tags.py` is the only module that may contain a `us-gaap` literal** (from M2),
   widening M1's `ingest/` rule to the whole package with a one-key allowlist. A tag literal
   anywhere else is the first line of a shadow tag table, and the failure mode of two tag tables
   is that the report and its own provenance line disagree about which tag won.
10. **No sort under `normalize/` or `report/` may use a partial key** (from M2). `FiscalPeriod`
    compares on `(end, kind)` with `start` excluded, so a stable sort over ties returns payload
    iteration order — deterministic in practice, not a guarantee, and invisible when wrong. The AST
    rule is blunt: it fails any `sorted`/`min`/`max`/`.sort` with no `key=`, because it cannot tell
    which sorts are safe. Sorting something already total — a list of dates — is written
    `key=identity` (`normalize/tags.py`), which states the claim rather than omitting it.
11. **Nothing under `normalize/` or `report/` reads a clock** (from M2). `as_of` is resolved at the
    command boundary — in the command's body module, next to `cli.py`, which is where `fetch.py` and
    `facts.py` do it — and threaded down. A `date.today()` in the pipeline makes two runs either side
    of midnight differ, which the determinism gate reports as a bug that isn't one. Which modules may
    read one is pinned by `test_layering::test_only_a_command_body_reads_a_clock` rather than listed
    here, because a list here goes stale the milestone after it is written.
12. **Determinism is a feature, and it is configured up front** (M3). `SOURCE_DATE_EPOCH`,
    pinned `svg.hashsalt`, `metadata={"Date": None}`, and the LLM response cache keyed on
    `(prompt version, document hash, model id)` so the LLM path is inside the determinism gate
    rather than exempt from it. Two runs must produce a byte-identical PDF. `report.json` is
    already under the gate from M2.

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
- [ ] No `us-gaap` literal outside `normalize/tags.py`; no clock read, `float`, or keyless sort
      under `normalize/` or `report/`.
- [ ] `make check` passes.
