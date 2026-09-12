---
name: mcp-atlassian-better
description: Look up, search, or update Jira issues across several Jira Cloud sites through the mcp-atlassian-better server, download or upload attachments straight to disk, and delete a comment. Use for requests like "look at BETA-2274", "get ACME-13447", "what's the status of GAM-979", "download the screenshots on ACME-13447", "attach this file to BETA-2274", "add a comment to ACME-13447", or "delete that comment I just added to PROJ-123" — anything naming a Jira issue key or asking to search/browse Jira.
---

# mcp-atlassian-better

One MCP server, several Jira Cloud sites. It figures out which site an issue
belongs to from its key prefix, so you almost never need to say which site
you mean.

## Start of session: call `jira_sites` once

Before the first Jira tool call in a session, call `jira_sites()`. It costs
nothing and tells you:

- Which sites are configured and their `key_prefixes` (e.g. `acme: ACME,
  ACMEH, ACMI | beta: BETA | gamma: GAM`) — this is how you know which
  prefix maps to which site without guessing.
- Which sites are actually `healthy` right now vs `failed` — if a site you
  need is `failed`, say so rather than retrying silently; point at its
  `error`/`log_path`.
- `read_only` per site — a write call (add comment, create issue, upload
  attachment) against a `read_only` site will be refused; don't attempt one
  without checking this first, and don't treat the refusal as a bug.
- `source` per site — which file actually defined it (`config.toml`, a
  `sites.d/` drop-in file another tool added, or `"env"`). Useful when a
  site's behavior is surprising and you need to know where its config comes
  from.

Sites can also arrive from a `sites.d/` drop-in directory (another tool
adding a site without editing the user's `config.toml`) in addition to
`config.toml` itself — you don't need to do anything differently, just be
aware `source` may point at either.

## Omit `site` whenever an issue key is in your arguments

Every mirrored tool, the three attachment tools, and `jira_delete_comment`
accept an optional `site` parameter, but it's inferred automatically from any
issue key in the call
(`issue_key`, `epic_key`, `parent`, and similar arguments) — just pass
`BETA-2274` or `ACME-13447` and let it route. Only pass `site` explicitly when
you already know it or the call has no issue key to infer from.

**Always pass `site` explicitly for `jira_search` or any other JQL-only
call.** JQL text is never parsed for routing (even when it contains
something that looks like an issue key) — a bare JQL search without `site`
will be refused with an error listing the configured sites; don't try to
work around this by embedding a key in the JQL, just pass `site`.

## Attachments: download to disk, then `Read` the path

For "download the screenshots on ACME-13447" or similar: call
`jira_download_attachments(issue_key=..., target_dir=...)`, using an
**absolute** `target_dir` (a relative one resolves against the server
process's own working directory, not yours). Then use your normal file-read
tool on the returned path(s). Don't reach for any inline/base64 attachment
content — it doesn't exist here on purpose; this server always writes to
disk instead. Pass `filenames` (or `attachment_ids`) to select specific
attachments instead of pulling everything — e.g. "download the screenshots"
should pass the PNG filenames rather than also pulling a 15 MB log file
attached to the same issue. Check the result's `skipped`/`failed` lists — a
filename collision or an over-size file lands there, not in `downloaded`.

For "attach this file to BETA-2274": call `jira_upload_attachments(issue_key=...,
paths=[...])` with absolute local paths.

## Deleting a comment is permanent — use it to clean up a mistake, nothing else

`jira_delete_comment(issue_key=..., comment_id=...)` permanently deletes one
comment. **There is no undo.** Use it when asked to remove a comment that was
added by mistake (e.g. right after `jira_add_comment`, or when the user
explicitly says "delete"/"remove" a comment) — never to "edit" a comment,
which is `jira_edit_comment` instead. `comment_id` must be the numeric id
(from `jira_get_issue`'s comment list, or the id `jira_add_comment` just
returned); anything else is refused before any request is sent.

## Reading errors

- An error text prefixed `[site=x]` names the site the call actually ran
  against — use it to tell which child failed, especially when the call had
  no explicit `site` and you're unsure what was inferred.
- A tool result with a `failed[]` or `skipped[]` list (`jira_download_attachments`
  only) describes per-item problems without failing the whole call — read the
  `reason` field on each entry rather than assuming the whole operation
  succeeded or failed as a unit.
- "site is unavailable" or a `read_only = true` refusal are both expected,
  reportable states — not something to retry in a loop.

## Never paste a token

Never echo, log, or repeat back an API token, personal access token, or any
value from a site's config file — not even redacted-looking fragments. If
something needs a token (setup, troubleshooting `--check` failures), point
the user at `id.atlassian.com/manage-profile/security/api-tokens` and let
them handle it directly.
