# mcp-atlassian-better — notes for Claude Code sessions in this repo

## Git identity and GitHub account

All commits are authored as `Ben Grossman <13681950+quaz579@users.noreply.github.com>` and
all `gh` and push operations use the `quaz579` GitHub account. Before any commit or `gh`
command, run `gh auth status` (must show `quaz579` active) and `git config user.email` (must
show the address above). Never switch to another account. Repo-local git config and local
`pre-commit` / `pre-push` hooks enforce this; if a hook rejects you, fix the identity, do not
bypass it with `--no-verify`.

## This is a public repo

Never commit real Jira site URLs, project keys, issue keys, account ids, cloud ids, tokens, or
local filesystem paths. Use placeholders such as `example.atlassian.net` and `PROJ-123`.
Never read or print a user's `~/.config/mcp-atlassian-better/config.toml`; it holds a live token.

## Before pushing

Run exactly what CI runs (`.github/workflows/ci.yaml`):

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests scripts
uv run pytest -m "not integration"
uv build
uvx --from . mcp-atlassian-better --help
```

Commits follow Conventional Commits; PR titles are checked by `.github/workflows/pr-title.yaml`.
