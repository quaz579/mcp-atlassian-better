# Contributing

## Commit messages

This repo uses [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): message`, where `type` is one of `feat`, `fix`, `chore`, `docs`,
`ci`, `test`, `refactor`, or `build`. The scope is optional. Dependabot opens its
PRs as `build(deps): ...`, which is why `build` is in the list.

## Pull requests

Open pull requests against `main`. The PR title must itself be a valid
Conventional Commit message; CI checks this.

## Local development

```bash
uv sync --all-groups
uv run ruff format .        # auto-fixes formatting; CI only runs the check below
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests scripts
uv run pytest -m "not integration"
uv build
uvx --from . mcp-atlassian-better --help
```

CI (`.github/workflows/ci.yaml`) runs every one of these *except*
`ruff format .` itself (that line is a local convenience to auto-fix; CI only
ever checks formatting, never rewrites it), on Python 3.10 and 3.13 —
matching the rest locally before pushing saves a round trip.

## Running the integration suite

`tests/integration/test_live_sites.py` drives the real server against your
real, configured Jira sites (no mocks) — it's skipped by default. It needs:

- A working config (`MCP_ATLASSIAN_BETTER_CONFIG`, or the default
  `${XDG_CONFIG_HOME:-~/.config}/mcp-atlassian-better/config.toml`), verified with
  `mcp-atlassian-better --check` first.
- `MCP_ATLASSIAN_BETTER_INTEGRATION=1` to opt in.
- `MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE=<KEY>` (a real, writable issue you own and
  are happy to receive and lose test attachments) to additionally run the
  upload → list → download → delete round trip. Without it, that write round
  trip is skipped (reported, not silently vanished); the suite still runs its
  read-only checks (`jira_sites`, one `jira_get_issue` / `jira_list_attachments`
  per configured site) either way.

```bash
MCP_ATLASSIAN_BETTER_INTEGRATION=1 MCP_ATLASSIAN_BETTER_TEST_SANDBOX_ISSUE=<an issue you own> uv run pytest -m integration
```

Never `cat`/`grep`/`sed` your own `config.toml` in a transcript an agent or a
CI log might capture — it holds a real credential. Use `--print-config`
(secrets always masked) to inspect it instead.

## Adversarial review expectation

A PR here is expected to go through a review-and-fix loop (a rigorous code
review plus, for anything runtime-dependent, real execution — not just
mocked unit tests) before it's considered done. Expect review comments that
ask for real `--check`/integration evidence, not just "the unit tests pass."
