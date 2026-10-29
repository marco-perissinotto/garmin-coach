# Contributing to garmin-coach

Thanks for your interest. garmin-coach is a small personal project: a local, read-only cache of one person's Garmin
Connect data, turned into context for an AI coach through an MCP server. Contributions are welcome as long as they
keep that shape.

## How to contribute

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach before you write
   code.
2. **Fork** the repository and create a branch from `main`.
3. Make your change with tests, then open a **pull request** against `main`.
4. CI must be green (ruff, mypy and the test suite on Linux, Windows and macOS). A maintainer reviews and merges;
   nobody pushes to `main` directly.

## Development setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync                                   # dependencies, including dev tools
git config core.hooksPath .githooks       # pre-commit hook: refuses key- or token-shaped strings
uv run pytest                             # offline tests: no Garmin account needed
uv run ruff check src tests && uv run mypy
```

You do **not** need a Garmin account or an API key to contribute: every test runs against synthetic fixtures and a
fake Garmin client (`tests/fakes.py`). The single live test (`tests/test_live.py`) runs only with
`GARMIN_COACH_LIVE=1` and your own key; CI never runs it.

## Rules that are not negotiable

These come from [docs/PLAN.md §0](docs/PLAN.md); a pull request that breaks one of them will not be merged.

- **No secrets, no personal data.** Never commit an API key, a bearer token, a database, logs, or real health data.
  Test fixtures must be synthetic or fully sanitized (no names, IDs, GPS positions, dates of birth). Do not paste such
  data in issues or pull requests either.
- **Read-only towards Garmin.** Only `GET` requests (the token exchange is the single `POST`), and only the 26
  endpoints documented in `docs/garmin-personal-api-openapi.json`.
- **Local only.** No telemetry, no cloud services, no third-party SaaS. Network servers bind to `127.0.0.1` only.
- **Use the official Garmin client** (`garmin-connect-personal-api-python`) for authentication.
- **Be polite to the API.** Any new call goes through the existing client wrapper (budget, backoff, circuit breaker)
  and the freshness gate; no polling, no background schedulers.
- **Deterministic pipeline.** Metrics and context packs are plain code; no LLM calls inside garmin-coach.

## Code conventions

- Match the surrounding code: type hints everywhere, `ruff` (line length 120) and `mypy` clean.
- Every behaviour change comes with a test. Tests stay offline and fast.
- Design choices that deviate from the plan, or that someone would otherwise have to rediscover, get a short entry in
  [docs/DECISIONS.md](docs/DECISIONS.md) (context, decision, consequence).
- Metric formulas are documented in [docs/METRICS.md](docs/METRICS.md); update it when you change one.
- Code, comments and docs in English. The context packs and the README are in Italian; English pack templates are a
  welcome contribution.

## Reporting a security problem

Please do not open a public issue: see [SECURITY.md](SECURITY.md).

## License of contributions

By opening a pull request you agree that your contribution is released under the project's license (see `LICENSE`).
