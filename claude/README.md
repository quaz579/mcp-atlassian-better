# Claude Code skill: `mcp-atlassian-better`

`skills/mcp-atlassian-better/SKILL.md` teaches Claude Code how to use the
`mcp-atlassian-better` server well — calling `jira_sites` first, omitting `site`
when an issue key is present, always passing `site` for JQL-only searches,
and preferring `jira_download_attachments` + a file read over any inline
attachment content.

## Install it

Copy the skill directory into your own `.claude/skills/`:

```bash
mkdir -p ~/.claude/skills
cp -R claude/skills/mcp-atlassian-better ~/.claude/skills/mcp-atlassian-better
```

Or, for a single project, into that project's `.claude/skills/` instead of
the global one. Either way, `mcp-atlassian-better` itself must also be registered
as an MCP server (see the main `README.md`) — the skill only changes how the
model *uses* tools that are already available; it doesn't add the server.

A future option is distributing this skill as part of a Claude Code plugin
marketplace entry instead of a manual copy — not done yet, out of scope for
this milestone.
