# M3 — The `analyze` command

`src/investo/analyze.py`. The body lives at the package root next to `fetch.py` and `facts.py`,
for the reason both of those give: `cli.py` stays the declared flag surface and nothing else,
because `tests/test_cli_surface.py` reads that file against README § Usage in both directions and
orchestration in the middle would make the check harder to trust.

The name collides with DESIGN §3.1's `analyze/` package for M4 and that is
[`README.md` § 7 question 1](README.md#7-spec-questions), not something to resolve here.

---

## 1. `run_analyze`

```python
def run_analyze(
    ticker: str,
    *,
    settings: Settings,
    refresh: bool = False,
    as_of: date | None = None,
    brief: bool = False,
    explain: bool = False,
    peers: tuple[str, ...] | None = None,
    assumptions: Path | None = None,
    version: str = "0.1.0",
) -> AnalyzeOutcome: ...
```

The first four lines are `run_facts`'s, deliberately — one fetch path, one normalization path, one
clock read:

```python
years    = parse_lookback(settings.lookback)
resolved = as_of if as_of is not None else date.today()   # here, once, and nowhere below
span     = lookback_window(years, as_of=resolved)
result   = run_fetch(ticker, settings=settings, refresh=refresh, as_of=resolved)
history  = build_history(..., coverage_floor=settings.coverage_floor)
envelope = run_info(settings, ..., brief=brief, explain=explain, peers=peers, assumptions=…)

model    = build_model(history, envelope, brief=brief)
document = serialize(history, run=envelope)
pdf      = render_pdf(model, brief=brief, source_date_epoch=_epoch(resolved))
```

`analyze.py` joins `cli.py`, `fetch.py` and `facts.py` in
`test_layering::COMMAND_CLOCK_READ_ALLOWED`. That set is pinned by two tests and one of them
asserts every member sits at the package root — which is what makes the collision in question 1 a
constraint rather than a preference.

**Charts are built inside `build_model`, not here.** The command orchestrates; it does not know
that a report has five charts. That also keeps `analyze.py` free of a matplotlib import, so the
command-level tests do not pay for one.

---

## 2. The output contract

```
{out_dir}/{TICKER}/{as_of}/report.pdf
{out_dir}/{TICKER}/{as_of}/report.json
```

README fixes the two file names and their adjacency (*"Every run also writes `report.json` next to
the PDF"*) and says nothing above that. Three candidate layouts, and only one survives:

| Layout | Fails on |
|---|---|
| `reports/report.pdf` | the second ticker overwrites the first |
| `reports/AAPL.pdf` | tomorrow's run overwrites today's, destroying the input `investo diff` exists for (§4.5) |
| `reports/AAPL/2026-08-01/…` | — |

Keyed by `as_of` rather than by wall-clock run time, so **re-running the same point-in-time
reconstruction overwrites itself**. That is what makes §11's gate runnable as "run it twice and
compare the file" rather than needing a temp directory per run, and it means a directory listing
is a list of distinct reconstructions rather than a list of times someone typed the command.

`TICKER` is the resolved symbol, upper-cased by `_resolve_ticker`, so `investo analyze aapl` and
`investo analyze AAPL` write to one place.

**Write order: `report.json` first, then `report.pdf`.** If the render raises, the run leaves the
machine-readable half of a documented pair on disk, which is recoverable; the reverse leaves a PDF
whose run record does not exist, which is the artifact §3.2's traceability claim is about. Both
are written with `os.replace` from a temporary file in the same directory, so a partial file is
never visible under the final name — a half-written `report.json` that parses is worse than one
that does not.

---

## 3. `AnalyzeOutcome` and exit 3

```python
@dataclass(frozen=True, slots=True)
class AnalyzeOutcome:
    ticker: str
    pdf_path: Path
    json_path: Path
    pages: int
    exit_code: ExitCode
    reason: str | None          # why the code is not 0
    findings: int
    coverage_tier1_annual: Decimal | None
```

**The code is a field, not an exception.** §14 says exit 3 is *"insufficient data, **report still
written**"*, and a function that raises `InsufficientDataError` before returning cannot have
written anything. Returning it makes the ordering structural: the files exist, the command reports
that they are thin, and the promise §14 makes is kept by construction rather than by remembering
to write before raising.

Two triggers, and one non-trigger that is the whole point
([`README.md` § 4](README.md#4-exit-codes)):

- `companyfacts` absent for the CIK → 3. There is a report; it has no financials in it and says so.
- Tier-1 **annual** fill rate below `settings.coverage_floor`, where one is configured → 3.
- The forecast, peer, quality and narrative sections being absent → **0**. An unbuilt milestone is
  not insufficient data.

`coverage_floor` is a new `Settings` field, `Decimal | None`, **defaulting to `None`** — no floor.
§4.2 sanctions *a configurable floor* and supplies no number; `docs/m2/COVERAGE.md` is the
measurement that would, and it does not exist. A default invented now fires arbitrarily and would
be tuned by whoever it annoyed first. The same posture as `pyproject.toml`'s unset `fail_under`,
and it should be resolved the same way: measure, then set, and put the measured figure in the
commit message.

Annual rather than quarterly, because the five M3 charts are annual and the criterion should
measure what the report actually contains.

---

## 4. The flags that belong to later milestones

| Flag | Behaviour | Why |
|---|---|---|
| `--llm anthropic\|openai\|gemini` | **exit 5**, naming M6 | A report that silently omits the section the user asked for is worse than one that refuses. Exit 5 is "config error", and requesting a provider that does not exist is one. `--llm none` is the default and passes. |
| `--peers MSFT,GOOG` | validated, recorded in `run.peers`; section 3 states the omission | M4 owns the cohort. It is a real input to a later run and belongs in the run record now, so a re-run at M4 can be compared against it. |
| `--assumptions FILE` | existence-checked (M0) **and parsed as TOML**; exit 5 if it is not; recorded in `run.assumptions` as the path | M5 owns the contents. Parsing costs nothing and moves the failure from after a 40-second fetch to before it. The path, not the contents, goes in the run record — the file may hold anything at M5 and the record should not have to grow a schema per milestone. |
| `--explain` | recorded in `run.explain`; **no other effect** | There are no intermediate calculations until M5. [Question 9](README.md#7-spec-questions); a test asserts the PDF bytes are unchanged by it. |
| `--brief` | selects the template | [`03-templates.md` § 5](03-templates.md#5---brief) |

Adding four keys to `report.json`'s `run` block is not a `schema_version` bump — §4.5's rule is
that adding a key is not, and changing a key's type, units or meaning is.

---

## 5. The summary the command prints

Exit code and file paths are not enough: someone who just waited forty seconds should be told what
they got.

```
AAPL  Apple Inc.  CIK 320193             as of 2026-08-01

  report.pdf      14 pages          reports/AAPL/2026-08-01/report.pdf
  report.json     schema 1          reports/AAPL/2026-08-01/report.json

  coverage        tier 1 annual 92.9%   tier 2 annual 78.6%   spine: filings
  history         11 annual, 44 quarterly periods
  charts          5 rendered, 0 omitted
  findings        3   (q4_derived, series_stitched, restated)

  not in this report: forecast (M5), quality scores and peers (M4),
                      8-K events and filing diffs (M4.5), narrative risk (M6)
```

Three properties, matching `facts.py`'s reasoning about its own table:

- **Omissions are counted and named**, not left for the reader to notice in the PDF. `0 omitted` is
  printed rather than suppressed, because a line that only appears when something is wrong is a
  line nobody learns to read.
- **The absent milestones are listed on every successful run.** This is the summary's most useful
  line for the next several milestones and the one most likely to be cut as noise. It is what stops
  someone concluding the tool has no opinion on valuation when it simply has not been built yet.
- **Findings are named, not just counted.** "3 findings" is a number nobody acts on — the same
  argument `facts.py` makes, one command over.

On exit 3 the summary still prints, with the reason as its last line, because the files exist and
the point of the code is that they do.

---

## 6. `cli.py`'s change

The `analyze` body loses its `NotImplementedYetError` and gains the wiring in
[§ 1](#1-run_analyze). Four tests in `tests/test_cli_surface.py` change with it, and they are
listed here because three of them are subtle:

| Test | Change |
|---|---|
| `test_stub_exits_70_and_names_its_milestone` | the `("analyze AAPL", "M3")` row is removed; only `backtest`/M7 remains |
| `test_implemented_commands_do_not_report_not_implemented` | gains `["analyze", "AAPL"]` |
| `test_stub_is_reached_only_after_config_resolves` | its subject moves from `analyze` to `backtest` — the property is about ordering and needs a subject that is still a stub, which is the third time this test has moved and is the intended lifecycle |
| `test_as_of_today_is_accepted`, `test_peers_accepts_a_comma_separated_list`, `test_main_returns_the_exit_code` | all three assert `NOT_IMPLEMENTED` for `analyze`; each moves to `backtest` or asserts a non-70 code |

**When `backtest` lands at M7, `NotImplementedYetError` and `ExitCode.NOT_IMPLEMENTED` go with
it** — ROADMAP § Decided during design: *"It disappears with the last stub."* M3 removes the
second of three; the test file should say which one is left.
