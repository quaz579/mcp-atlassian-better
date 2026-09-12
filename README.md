# mcp-atlassian-better

One MCP server that talks to several Jira Cloud sites at once. Ask Claude Code
to "look at BETA-2274" or "get ACME-13447" and it reaches the right site
automatically, with no `site` argument needed. It also downloads and uploads
attachments straight to disk, instead of the base64-in-chat-context upstream
gives you.

> **Migrating from `jira-multi-mcp`?** These renames are silent breakers — nothing
> errors, your old settings just stop being read:
> - Config dir: `~/.config/jira-multi-mcp` → `~/.config/mcp-atlassian-better`.
> - Env vars: `JIRA_MULTI_CONFIG` / `JIRA_MULTI_SITES_DIR` /
>   `JIRA_MULTI_SITE_<NAME>_*` / `JIRA_MULTI_INTEGRATION` /
>   `JIRA_MULTI_TEST_SANDBOX_ISSUE` → `MCP_ATLASSIAN_BETTER_*` (same suffixes).
> - Log dir: `~/.local/state/jira-multi-mcp/logs` →
>   `~/.local/state/mcp-atlassian-better/logs`.
> - Response `_meta` keys: `jira-multi-mcp/site` and `jira-multi-mcp/site_reason` →
>   `mcp-atlassian-better/site` and `mcp-atlassian-better/site_reason`.

It works by spawning the upstream [`sooperset/mcp-atlassian`](https://github.com/sooperset/mcp-atlassian)
server unmodified, one child process per configured site, and mirroring its
tools behind a `site` selector:

```
Claude Code ──stdio──▶ mcp-atlassian-better (parent)
                          │  one tool set: jira_get_issue(site?, ...) etc.
                          │  + jira_sites, jira_list_attachments,
                          │    jira_download_attachments, jira_upload_attachments,
                          │    jira_delete_comment
                          │
                          ├─▶ child "acme":  uvx mcp-atlassian   (ACME, ACMEH, ACMI)
                          ├─▶ child "beta":  uvx mcp-atlassian   (BETA)
                          └─▶ child "gamma": uvx mcp-atlassian   (GAM)

  site resolution: explicit `site` arg ▶ else the only configured site ▶ else
  the project-key prefix found in issue_key / issue_keys / epic_key / parent /
  project_key / ... (never `jql` — free-text JQL is never parsed for routing)
```

No fork of upstream, so its bug fixes and new tools flow straight through —
see [How it stays current with upstream](#how-it-stays-current-with-upstream).

Status: pre-alpha, under construction.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) (which provides `uvx`) on your
`PATH`; `mcp-atlassian-better` itself and the upstream `mcp-atlassian` child are
both run through it, so nothing else needs a manual `pip install`.

**This project is not yet published to PyPI.** Once it is, install/run it
with:

```bash
uvx mcp-atlassian-better@latest
```

Until then, install straight from this git repo — every command below that
runs `uvx --from git+https://github.com/quaz579/mcp-atlassian-better mcp-atlassian-better`
is that form; swap it for `uvx mcp-atlassian-better@latest` once a release ships.

Register it with Claude Code:

```bash
claude mcp add -s user jira -- uvx --from git+https://github.com/quaz579/mcp-atlassian-better mcp-atlassian-better
```

Or add it directly to a project's `.mcp.json`:

```json
{
  "mcpServers": {
    "jira": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/quaz579/mcp-atlassian-better", "mcp-atlassian-better"]
    }
  }
}
```

Or to Claude Desktop's `claude_desktop_config.json` (same shape, under
`mcpServers`). Claude Desktop does **not** inherit your shell environment, so
if a site's token is set via `api_token_env`, add an `"env"` block naming that
variable — or use a literal `api_token` in the config file instead (0600
permissions, see below).

Before wiring it into an agent, run the config through `--check` once from a
terminal — it authenticates against every configured site and exits non-zero
if any fail, which is much easier to read than an agent's first confused tool
call:

```bash
uvx --from git+https://github.com/quaz579/mcp-atlassian-better mcp-atlassian-better --check
```

## Configuration

Copy `config.example.toml` to `${XDG_CONFIG_HOME:-~/.config}/mcp-atlassian-better/config.toml`
(`chmod 600` recommended — a world/group-readable file still loads, but
mcp-atlassian-better logs a warning) and fill in your sites. Override the location
with the `MCP_ATLASSIAN_BETTER_CONFIG` environment variable or `--config PATH`.

### `[defaults]` — fallback values for every site that doesn't set its own

| Key | Default | Notes |
|---|---|---|
| `username` | none | Required for Cloud auth (`api_token`/`api_token_env`); not needed for Server/DC (`personal_token`). |
| `api_token` | none | A literal Cloud API token. Mutually exclusive with `api_token_env` and with either `personal_token*`. |
| `api_token_env` | none | Name of an environment variable holding the Cloud API token. Preferred over `api_token` — a literal value logs an INFO line suggesting the env form each time it's used. |
| `personal_token` / `personal_token_env` | none | Same idea, for a Jira Server/Data Center Personal Access Token (Bearer auth). Mutually exclusive with the `api_token*` pair. |
| `toolset_preset` | `"curated"` | `"curated"` mirrors the 37 hand-picked tools below (36 mirrored directly, plus `jira_download_attachments`, which the wrapper shadows with its own disk-writing version); `"all"` mirrors every `jira_`-prefixed tool upstream exposes (agile boards/sprints, service desk, forms, dev-info, and more — not individually verified by this repo). |
| `call_timeout_seconds` | `120` | Per-call timeout to a child before the tool call fails with a `[site=...]` error naming the child's log. |
| `connect_timeout_seconds` | `90` | How long to wait for each child to finish starting up. |
| `attachment_max_bytes` | `104857600` (100 MiB) | Per-file cap enforced by `jira_download_attachments`, checked against both the reported `Content-Length` and the actual streamed byte count. |
| `recovery_cooldown_seconds` | `30` | How long a site that's `failed` stays marked that way before the next call that needs it (or `jira_sites`) tries to reconnect it. See [How site recovery works](#how-site-recovery-works). |
| `health_recovery_budget_seconds` | `8` | Upper bound on how long one `jira_sites` call waits for recovery to settle before returning — a slower recovery keeps running in the background and is picked up by a later call. See [How site recovery works](#how-site-recovery-works). |

### How site recovery works

A site is marked `failed` after a single dropped connection, or after 3
consecutive per-call timeouts in a row (one slow call doesn't take a site
down by itself). A `failed` site — including one that failed at startup — is
retried automatically once `recovery_cooldown_seconds` has elapsed since the
last failure: either by the next tool call routed to it, or by the next
`jira_sites` call.

`jira_sites` reports each site's `state` (a site whose recovery attempt is
actively in flight shows `recovering`, not `failed`), a lifetime
`recovery_attempts` counter, and an estimated `next_retry_at`. Its own
recovery pass — and any newly-mirrored tools that requires (see below) — is
bounded by `health_recovery_budget_seconds`: a recovery attempt that's still
running when the budget expires keeps going in the background and is picked
up by the next `jira_sites` call, rather than making that call wait
indefinitely.

If every configured site was `failed` at startup, no tools were ever
mirrored (there's nothing to discover them from). Once at least one site
recovers, `jira_sites` mirrors the real tools for the first time; the
connected client is notified via a `tools/list_changed` message where the
transport supports it, or — if that notification can't be sent — the
`jira_sites` response carries a `note` asking the caller to re-list tools.

### `[upstream]` — how the child `mcp-atlassian` process is launched

| Key | Default | Notes |
|---|---|---|
| `command` | `["uvx", "mcp-atlassian@latest"]` | The `@latest` suffix forces `uvx` to revalidate against PyPI on every launch (a cheap conditional request, not a full re-download unless the version actually changed) — see [How it stays current with upstream](#how-it-stays-current-with-upstream). Pin a version for reproducibility instead, e.g. `["uvx", "mcp-atlassian==0.23.1"]`. |
| `env_passthrough` | `["SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"]` | Extra environment variables forwarded to each child. The child's own env is otherwise minimal — just `PATH` and `HOME`, plus whatever `mcp-atlassian-better` sets for that site (its Jira URL, credentials, `TOOLSETS`, etc.) — no ambient secret leaks in by default. |
| `workspace_dir` | `"."` | Working directory for the child process. |

### `[[sites]]` — one entry per Jira instance

Each entry needs a unique `name` (lowercase letters/digits, then any run of
lowercase letters/digits/`_`/`-`; it can't start with `_` or `-`), an
`https://` `url`, and `key_prefixes` — the project-key prefixes (e.g. `ACME`,
`BETA`) used to route a bare `ACME-1234` to the right site with no `site`
argument. **Every prefix across every site must be globally unique** — that's
a load-time error otherwise.

| Key | Default | Notes |
|---|---|---|
| `username`, `api_token`/`api_token_env`, `personal_token`/`personal_token_env` | inherited from `[defaults]` | Same rules as `[defaults]`; a site that sets none of these inherits whichever auth shape `[defaults]` defines. |
| `read_only` | `false` | Refuses every write tool against this site with a clear `read_only = true` error. |
| `enabled_tools` | none (no restriction beyond `toolset_preset`) | An explicit allowlist for this site. Under `toolset_preset = "curated"` it must be a subset of the curated tools; under `"all"` it just needs every entry to be `jira_`-prefixed. Enforced on **both** sides — the child's own `ENABLED_TOOLS` env var, and a wrapper-side check — so a misbehaving child can't serve a tool outside the allowlist. |
| `projects_filter` | none | Restricts which projects **this site's own upstream child** will search/browse at all (its static `JIRA_PROJECTS_FILTER` env var). Entries are normalized to uppercase project keys; a numeric project id is never accepted here (upstream's own JQL interpolation does accept one — this is stricter on purpose). Distinct from a tool call's own `projects_filter` *argument*, which only affects site *routing*, never enforcement. |

See `config.example.toml` for a filled-in, commented example, including a
Server/Data Center site (`personal_token_env`, no `username` needed).

### Drop-in site config (`sites.d/`)

Another tool (a dashboard, an installer) can add sites without editing your
`config.toml`: any `*.toml` file placed in `sites.d/` next to `config.toml`
(override the directory with `MCP_ATLASSIAN_BETTER_SITES_DIR`) is loaded as extra
`[[sites]]` entries, in lexical filename order, before `config.toml` itself —
so a site you also define in `config.toml` overrides the drop-in version
field by field. A drop-in file may contain **only** `[[sites]]` — no
`[defaults]`/`[upstream]`, and no literal `api_token`/`personal_token` (use
`api_token_env`/`personal_token_env` instead; only `config.toml`, which you
control directly, is trusted with a literal secret). A missing `sites.d/`
directory is fine — it just contributes no sites.

### Environment variable overlay

A `MCP_ATLASSIAN_BETTER_SITE_<NAME>_<FIELD>` variable overrides one field of a site
already defined in the TOML file. `<NAME>` is matched case-insensitively — it's
lowercased before comparing against the (already-lowercase) site name — so
`MCP_ATLASSIAN_BETTER_SITE_ACME_API_TOKEN_ENV` and
`MCP_ATLASSIAN_BETTER_SITE_acme_API_TOKEN_ENV` both target the site named
`acme`. A site name containing `-` can only be reached via the TOML file
(environment variable names can't contain a dash). A variable that would introduce a
**new** site the TOML file never defined is ignored (with a warning) unless
it supplies at least `_URL` and `_KEY_PREFIXES` — a stray/typo'd variable
should never silently create a broken site.

## Useful commands

- `mcp-atlassian-better --print-config` — show the effective, merged configuration
  with every secret masked as `***` (and where each token came from: the
  file, or an env-overlay variable), plus each site's `source` — which file
  (`config.toml`, a `sites.d/` drop-in, or `"env"`) actually defined it.
- `mcp-atlassian-better --check` — authenticate against every configured site and
  print a table of `displayName`/`accountId` per site, including its
  `source`; exits non-zero if any site fails (`--allow-partial` to tolerate
  some failures).
- `mcp-atlassian-better --warm [--refresh]` — run the upstream command once to
  prime `uvx`'s cache ahead of time; `--refresh` forces re-resolution instead
  of reusing a cached version.
- `mcp-atlassian-better` (no flags) — run the actual MCP server over stdio. This is
  what a client (Claude Code, Claude Desktop, `.mcp.json`) launches.

## How site resolution works

For a tool call with no explicit `site`: first, if only one site is
configured, that's the answer. Otherwise `mcp-atlassian-better` looks for a
project-key prefix in the call's own arguments — `issue_key`, `issue_keys`,
`epic_key`, `parent`, `inward_issue_key`, `outward_issue_key`,
`issue_ids_or_keys` (an issue key, e.g. `ACME-1234`), and `project_key`,
`target_project_key` (a bare project key). **`jql` is never parsed** for
routing, even though free-text JQL often contains something that looks like
an issue key — `jira_search` (and any other JQL-only call) always needs an
explicit `site`. A `projects_filter` argument (e.g. on `jira_search`) is also
inspected for a project-key token, but a purely numeric project id there is
silently skipped (never a routing signal, and not an error).

If no argument yields a recognizable key, or the arguments reference more
than one configured site, the call is refused with a clear error listing the
configured prefixes — pass `site` explicitly instead. An unknown prefix (one
no configured site owns) is also a clear error rather than a silent
mismatch.

## Tool set

`toolset_preset = "curated"` (the default) mirrors this hand-picked 37-tool
subset of upstream's Jira tools, one `MultiSiteProxyTool` per name, each with
an added optional `site` parameter. 36 are listed below; the 37th,
`jira_download_attachments`, is served by the wrapper's own disk-writing
version instead — see [Wrapper-owned tools](#wrapper-owned-tools).

| Area | Tools |
|---|---|
| Issues | `jira_get_issue`, `jira_create_issue`, `jira_batch_create_issues`, `jira_batch_get_changelogs`, `jira_update_issue`, `jira_assign_issue`, `jira_delete_issue`, `jira_move_issue` |
| Search / fields | `jira_search`, `jira_search_fields`, `jira_get_field_options`, `jira_get_create_fields`, `jira_get_project_fields` |
| Comments | `jira_add_comment`, `jira_edit_comment` |
| Transitions | `jira_get_transitions`, `jira_transition_issue` |
| Attachments (inline image content) | `jira_get_issue_images` |
| Users / watchers | `jira_get_user_profile`, `jira_search_assignable_users`, `jira_get_issue_watchers`, `jira_add_watcher`, `jira_remove_watcher` |
| Links | `jira_get_link_types`, `jira_create_issue_link`, `jira_create_remote_issue_link`, `jira_remove_issue_link`, `jira_link_to_epic` |
| Worklog | `jira_get_worklog`, `jira_add_worklog` |
| Projects | `jira_get_project_issues`, `jira_get_project_issue_types`, `jira_get_project_versions`, `jira_get_project_components`, `jira_get_all_projects`, `jira_search_projects` |

`toolset_preset = "all"` mirrors every `jira_`-prefixed tool upstream
exposes instead — agile boards/sprints, service desk, forms, and more — none
of which this repo has individually verified.

### Wrapper-owned tools

These five are implemented directly by `mcp-atlassian-better` rather than forwarded
to a child. The attachment tools and `jira_delete_comment` below all talk to
Jira Cloud REST v3 (`httpx`) directly and are **Cloud-only** — a Server/Data
Center site (`personal_token`) gets a clear refusal instead; `jira_sites` has
no such restriction and reports on every configured site regardless of auth
mode. Each of these four also echoes back a normalized `issue_key`
(stripped, uppercased) rather than the raw argument, so e.g. an input of
`" acme-1 "` comes back as `"ACME-1"`.

**`jira_sites()`** — no arguments. Per-site health: `name`, `host`,
`key_prefixes`, `read_only`, `enabled_tools_restricted`, `state` (`healthy`,
`failed`, or `recovering` — see [How site recovery works](#how-site-recovery-works)),
`error`/`last_error`/`last_error_at`, `timeouts`, `recovery_attempts`,
`next_retry_at`, `log_path`, `discovery_source` (whether that site was the
tool-discovery source), `source` (which file defined it — `config.toml`, a
[`sites.d/`](#drop-in-site-config-sitesd) drop-in file, or `"env"`), and
`fastmcp_server_version` — the FastMCP *library*
version the child reports, not upstream mcp-atlassian's own version (that's
the top-level `upstream_version`, probed once per process since every child
launches the same command). Never credentials. `healthy` means the child
process answered a liveness probe — **not** that its credentials or URL are
valid; run `--check` for that proof. A site actively recovering also carries
a `note` (e.g. "recovery in progress; call jira_sites again"). Call
`jira_sites` once per session to learn which prefixes exist and whether every
site is actually healthy.

**`jira_list_attachments(issue_key, site?)`** — read-only. Returns `id`,
`filename`, `size`, `mime_type`, `created`, `author` for every attachment on
the issue. Does not fetch content.

| Argument | Required | Notes |
|---|---|---|
| `issue_key` | yes | |
| `site` | no | Inferred from `issue_key`'s prefix if omitted. |

**`jira_download_attachments(issue_key, target_dir, site?, filenames?, attachment_ids?, overwrite?)`**
— writes attachments to disk and returns their paths; read the file from disk
afterward (with your file-reading tool) rather than expecting inline content.
This **shadows** upstream's own `jira_download_attachments`, which only
returns attachment content as base64 in-band — ours is registered instead,
so the base64 version is never mirrored and a caller can't accidentally pick
it.

| Argument | Required | Notes |
|---|---|---|
| `issue_key` | yes | |
| `target_dir` | yes | Use an **absolute** path — a relative one resolves against the server process's own working directory, not yours. The resolved absolute path is echoed back in the result either way. |
| `site` | no | Inferred from `issue_key`'s prefix if omitted. |
| `filenames` | no | Omit to download every attachment; an empty list is refused (omit the argument instead of passing `[]`). |
| `attachment_ids` | no | Same rule as `filenames`. |
| `overwrite` | no, default `false` | An existing same-named file is never silently clobbered: a collision falls back to `{name}-{attachment_id}{ext}` (also applied when two selected attachments share a name, regardless of `overwrite`), and if that also exists it's reported under `skipped` rather than written. `overwrite=true` replaces in place instead, via a temp file + atomic rename so a mid-download failure never truncates the file it would have replaced. |

A selector that matches nothing on the issue is reported under `failed`.
Anything selected but not written lands in `skipped` (name collision) or
`failed` (rejected by Jira, over `attachment_max_bytes`, an unsafe filename,
or a connection error). No directory is created if nothing was selected.
`downloaded[].filename` is the name actually written to disk;
`original_filename` carries Jira's own name when a collision changed it.

**`jira_upload_attachments(issue_key, paths, site?)`** — uploads local files
as new attachments and returns each created attachment's
`id`/`filename`/`size`/`mime_type`.

| Argument | Required | Notes |
|---|---|---|
| `issue_key` | yes | |
| `paths` | yes | Each must already exist as a regular file; use absolute paths — a relative one resolves against the server process's own working directory, not yours. |
| `site` | no | Inferred from `issue_key`'s prefix if omitted. |

A write operation: refused with a clear `read_only = true` error on a site
configured that way.

**`jira_delete_comment(issue_key, comment_id, site?)`** — permanently
deletes one comment. **Irreversible — there is no undo.** Upstream has no
delete-comment tool at all (only `jira_add_comment`/`jira_edit_comment`), so
this is a direct REST call like the attachment tools above. `comment_id`
must be the numeric comment id (digits only); anything else — a slash, a
query string, letters — is refused before any request is made. On success,
returns `{site, issue_key, comment_id, deleted: true}` — `issue_key` is
echoed normalized (stripped, uppercased), not the raw argument.

| Argument | Required | Notes |
|---|---|---|
| `issue_key` | yes | |
| `comment_id` | yes | Digits only. |
| `site` | no | Inferred from `issue_key`'s prefix if omitted. |

A write operation: refused with a clear `read_only = true` error on a site
configured that way.

`site` is optional on all five (except `jira_sites`, which takes no
arguments) and inferred from `issue_key`'s project prefix the same way every
mirrored tool resolves it.

## Per-site policy enforcement

Three settings, enforced differently depending on whether a tool is mirrored
(goes through a child) or wrapper-owned (talks to Jira directly):

| Setting | Mirrored (child) tool | Wrapper-owned tool |
|---|---|---|
| `read_only` | Entirely the child's job — baked in as its own `READ_ONLY_MODE` env var at launch. A write call against a read-only site gets the child's own refusal, plus a `(site 'x' is configured read_only = true)` hint appended when the tool's own annotations don't already mark it read-only. | Checked directly by the wrapper before the call runs. |
| `enabled_tools` | Enforced **twice**: the child's own `ENABLED_TOOLS` env var, *and* a wrapper-side check on every mirrored call — so a misbehaving child serving a tool outside its configured allowlist is still refused. | Checked directly by the wrapper. |
| `projects_filter` | Entirely the child's job — its own static `JIRA_PROJECTS_FILTER` env var restricts which projects it will search/browse at all. | Checked directly by the wrapper against the target issue's project (a numeric issue id is refused outright rather than resolved, since that would need an extra Jira call just to find out which project it belongs to). |

## Logs

Server logs go to stderr and to a rotating file under
`${XDG_STATE_HOME:-~/.local/state}/mcp-atlassian-better/logs/server.log` (5 MB × 5
backups, `chmod 600`), plus one `<site-name>.log` per child in the same
directory. Every log line — from `mcp_atlassian_better` itself, `httpx`,
`httpcore`, `mcp`, and `fastmcp` — passes through a redacting filter that
strips every configured token and any URL query string before it's written,
so a pre-signed attachment-download URL (which carries its own one-shot
`token=` parameter) is never logged even at `--verbose`. `httpx`/`httpcore`
are additionally capped at `WARNING` regardless of `--verbose`, since that's
otherwise where the outbound request line (including the full URL) gets
logged.

`--verbose` raises `mcp_atlassian_better`'s own log level to `DEBUG` (site
resolution reasoning, per-call detail); it does **not** lower the
`httpx`/`httpcore` cap above, and it never causes a token or a download URL's
query string to be logged — the redaction is unconditional.

## Shutdown behavior

Closing stdin (what Claude Code and Claude Desktop do when a session ends)
is the expected, silent shutdown path — the server notices within roughly
0.3-0.4 seconds and exits cleanly, tearing down every child first. A
`SIGTERM` (or `SIGINT`) is handled the same way and, when every child closes
cleanly, exits just as quickly (reporting success). A 5-second watchdog is
armed as a safety net on *every* shutdown — not only a signal, also the
ordinary "client closed stdin" path — so a hang tearing children down (e.g. a
child that ignores its own close request) still forces the process to exit
(reporting failure) rather than hanging indefinitely, since the MCP stdio
transport's own background stdin-reading thread can't always be canceled
promptly; any child still alive at that point gets `SIGKILL`ed via its whole
process group. Registered via `uvx` (the normal Claude Code registration
form), `uv` forwards the signal to the actual server process, so this is what
a supervisor's `kill <pid>` actually experiences.

## Pinning upstream / staying on `@latest`

The default `[upstream] command = ["uvx", "mcp-atlassian@latest"]` makes
`uvx` send a real (if cheap — a conditional HTTP request, not a full
re-download unless the version changed) check against PyPI on every launch,
so a newly published upstream release is picked up automatically. A bare
`uvx mcp-atlassian` (no `@latest`) is **not** equivalent — once `uv`'s local
HTTP cache for that package is warm, it's reused with no network call at
all, so a new release silently isn't picked up until that cache entry goes
stale. Pin a specific version instead for reproducibility:
`["uvx", "mcp-atlassian==0.23.1"]`. Either way, `mcp-atlassian-better --warm
[--refresh]` primes (or forces revalidation of) `uvx`'s cache ahead of a
cold start. See `docs/upstream-notes.md` for the full investigation.

## Troubleshooting

- **A site shows `failed` in `jira_sites`, or a tool call errors with `[site=x]
  site is unavailable: ... Run 'mcp-atlassian-better --check'.`** — run `--check`
  from a terminal; it authenticates directly and shows the real HTTP status
  and error body, which is much more specific than a tool-call failure. A
  site that failed to connect (at startup or later) stays `failed` for
  `recovery_cooldown_seconds` before it's tried again automatically — see
  [How site recovery works](#how-site-recovery-works).
- **`Unknown tool` with no other detail** — the tool name either isn't in
  the curated allowlist (check `toolset_preset`/`enabled_tools`), or no
  currently-healthy child advertised it at discovery time (a dead site at
  startup means its tools were never mirrored at all).
- **A 401 from a site** — the token is wrong, expired, or revoked; regenerate
  it at `id.atlassian.com/manage-profile/security/api-tokens` and confirm
  with `--check`.
- **A 404 where you expected the issue to exist** — Jira Cloud returns 404
  (not 403) for both "doesn't exist" and "exists but this account has no
  permission to see it" — cross-check by opening the issue in a browser as
  the same account before assuming it's a typo.
- **`site 'x' is configured read_only = true`** — expected on a site
  deliberately configured that way; not an error to "fix" unless the site
  should actually accept writes.

## Server / Data Center status

Server/Data Center sites (`personal_token`/`personal_token_env`) are
accepted by configuration and `--check`, and mirrored tools work against
them the same as Cloud. The three attachment tools are **Cloud-only** in
this version — a Server/DC site gets a clear refusal from each of them
rather than a confusing REST error.

## Security notes

- Prefer `api_token_env`/`personal_token_env` over a literal token in the
  config file; a literal value still works but logs an INFO reminder each
  time it's used.
- `chmod 600` the config file; a world/group-readable file loads anyway but
  logs a warning.
- `--print-config` masks every secret as `***` (and shows only where it came
  from — file or env-overlay variable — never the value).
- `jira_sites` and every error message are built to never include a
  credential.
- An attachment download follows Jira's redirect to a pre-signed media CDN
  URL; that URL (and its one-shot `token=` query parameter) is never logged,
  at any verbosity.

## How it stays current with upstream

No fork: `mcp-atlassian-better` spawns upstream `mcp-atlassian` as a subprocess and
only augments its tool schemas with an optional `site` parameter, so any
upstream bug fix or new tool is picked up automatically once it's
released — no changes needed here, other than curating a genuinely new tool
into `CURATED_TOOLS` if you want it mirrored under `toolset_preset =
"curated"` (it's already reachable under `"all"` without any code change).

A weekly GitHub Actions job (`.github/workflows/upstream-drift.yaml`) parses
upstream's `main` branch tool list and diffs it against what this repo knows
about (`tools_meta.CURATED_TOOLS`, `tools_meta.WRAPPER_OWNED_TOOLS`, and the
toolset tags verified at the time each was curated). If a currently-curated
tool disappears upstream, or a brand-new toolset shows up, it opens (or
updates) a single tracking issue — see `scripts/upstream_tool_inventory.py`.
