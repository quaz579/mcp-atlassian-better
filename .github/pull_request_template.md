## What does this change?

<!-- One or two sentences. -->

## Checklist

- [ ] PR title is a valid [Conventional Commit](https://www.conventionalcommits.org/) message (the `pr-title` check enforces this)
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests scripts` pass locally
- [ ] `uv run pytest -m "not integration"` passes locally
- [ ] Docs updated (README / CONTRIBUTING) if behavior or config changed
- [ ] No real credentials, hostnames, project keys, issue keys, or other live data in the diff
