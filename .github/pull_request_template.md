## What and why

<!-- What does this change do, and which issue does it address (e.g. "Closes #12")? -->

## How it was tested

<!-- Tests added or changed; anything checked by hand. -->

## Checklist

- [ ] `uv run pytest`, `uv run ruff check src tests` and `uv run mypy` pass locally
- [ ] No API keys, tokens, databases, logs or real health data in the diff (fixtures are synthetic)
- [ ] Garmin is still contacted only with `GET` on the 26 documented endpoints, through the client wrapper
- [ ] New design choices recorded in `docs/DECISIONS.md`; metric changes in `docs/METRICS.md`
