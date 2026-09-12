# Upstream `mcp-atlassian` uvx re-resolution behavior

Verified 2026-09-10 with `uv 0.9.26` on macOS, against `mcp-atlassian` version `0.23.1`
(the current PyPI release at test time). Commands run from an interactive shell, cache
warm from a prior `uvx mcp-atlassian` invocation unless noted.

## What was observed

| Command | Run | Wall time | Version resolved |
|---|---|---|---|
| `uvx mcp-atlassian --help` | 1st (cold, after `uv tool uninstall`) | 5.20s | 0.23.1 |
| `uvx mcp-atlassian --help` | 2nd (warm) | 0.575s | 0.23.1 |
| `uvx mcp-atlassian@latest --help` | 1st | 0.901s | 0.23.1 |
| `uvx mcp-atlassian@latest --help` | 2nd | 0.817s | 0.23.1 |
| `uvx --refresh mcp-atlassian --help` | 1 run | 0.783s | 0.23.1 |
| `uvx mcp-atlassian --help` (after `--refresh`) | warm again | 0.447s | 0.23.1 |

`uvx mcp-atlassian --version` printed `mcp-atlassian, version 0.23.1` throughout; the
version never changed across any of these runs, so this test does not distinguish "always
resolves the same because it's genuinely latest" from "resolves the same because it's
cached" on its own. The `-v` output below is what settles that question.

Ran `uv -v tool run --from mcp-atlassian mcp-atlassian --help` and
`uv -v tool run --from mcp-atlassian@latest mcp-atlassian --help` and grepped the debug
log for HTTP cache lines:

- Bare (no `@latest`), warm cache: `Found fresh response for: https://pypi.org/simple/mcp-atlassian/`
  for every dependency — uv serves the whole resolution from its local HTTP cache with
  **no network round trip at all** while the cached response is still within its
  freshness window.
- `@latest`, same cache state: `Found stale response for: https://pypi.org/simple/mcp-atlassian/`
  then `Sending revalidation request for: ...` then `Found not-modified response for: ...`
  — uv sends a real conditional HTTP request to `pypi.org` on **every** invocation to check
  for a newer release, even when the answer comes back unchanged.
- `uvx --offline mcp-atlassian --help` and `uvx --offline mcp-atlassian@latest --help`
  both succeeded from cache with no network access at all, confirming the resolution
  metadata for both forms was already cached locally.

This matches the timing: the `@latest` runs are consistently ~0.3-0.4s slower than the
bare warm runs, which is the cost of that revalidation round trip to PyPI (even when it
returns "not modified").

## What this means for the wrapper

- Bare `uvx mcp-atlassian` is **not** "always latest" on a warm cache: once uv's local
  HTTP cache entry for `pypi.org/simple/mcp-atlassian/` is fresh, uv reuses the resolved
  version with no network call, so a newly published upstream release will not be picked
  up until that cache entry goes stale (or `--refresh` is used).
- `uvx mcp-atlassian@latest` does force a real check against PyPI on every launch (a
  conditional request, so it costs a small round trip but not a full re-download unless
  the version actually changed), which is the behavior the default
  `[upstream] command = ["uvx", "mcp-atlassian@latest"]` in the plan is relying on.
- `uvx --refresh` forces the same kind of revalidation as `@latest` for one invocation
  without pinning `@latest` permanently in the command.

## Not verified

- How long uv's HTTP cache freshness window is for `pypi.org/simple/*` responses (not
  measured; PyPI's simple index sends its own cache-control headers and this was not
  inspected directly). So "bare uvx re-uses a stale version" is confirmed as a mechanism,
  not measured for a specific duration.
- Behavior against a private/self-hosted index rather than public PyPI.
