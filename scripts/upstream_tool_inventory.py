"""Diffs upstream mcp-atlassian's live Jira tool list against what this repo
curates, and (optionally) files or updates a single GitHub tracking issue
when something worth a human look has changed.

Run locally to see the report: ``uv run python scripts/upstream_tool_inventory.py``.
The weekly ``.github/workflows/upstream-drift.yaml`` job runs it with
``--create-issue``, which additionally requires the ``gh`` CLI and a
``GH_TOKEN``/``GITHUB_TOKEN`` in the environment.

Tool names are recovered by AST-parsing upstream's own
``src/mcp_atlassian/servers/jira.py`` for ``@jira_mcp.tool(...)``-decorated
functions, rather than importing upstream (which would require installing
its full dependency set just to introspect it). Verified against both
upstream's ``main`` and its ``v0.23.1`` tag on 2026-09-11: identical 63-tool,
16-toolset-tag set — this script's baseline below is that verified snapshot.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mcp_atlassian_better.tools_meta import CURATED_TOOLS, WRAPPER_OWNED_TOOLS

UPSTREAM_REPO = "https://github.com/sooperset/mcp-atlassian"
UPSTREAM_JIRA_SERVER_PATH = "src/mcp_atlassian/servers/jira.py"
ISSUE_TITLE = "upstream drift: mcp-atlassian tool list changed"

# The toolset tags (``toolset:jira_*``) verified present on both upstream's
# ``main`` and its ``v0.23.1`` tag on 2026-09-11. A tag appearing that isn't
# in this set means upstream shipped a whole new toolset since this baseline
# was last updated — worth a look for possible curation, even though none of
# its individual tool names were previously known (so the "new tool" diff
# below wouldn't otherwise call it out as anything special).
BASELINE_TOOLSET_TAGS: frozenset[str] = frozenset(
    {
        "toolset:jira_agile",
        "toolset:jira_attachments",
        "toolset:jira_comments",
        "toolset:jira_development",
        "toolset:jira_fields",
        "toolset:jira_forms",
        "toolset:jira_issues",
        "toolset:jira_links",
        "toolset:jira_metrics",
        "toolset:jira_project_analysis",
        "toolset:jira_projects",
        "toolset:jira_service_desk",
        "toolset:jira_transitions",
        "toolset:jira_users",
        "toolset:jira_watchers",
        "toolset:jira_worklog",
    }
)

# CURATED_TOOLS already includes "jira_download_attachments" (the one
# wrapper-owned tool with a real upstream counterpart -- it's deliberately
# shadowed, see tools_meta.WRAPPER_OWNED_TOOLS). The other four
# wrapper-owned tools (jira_sites, jira_list_attachments,
# jira_upload_attachments, jira_delete_comment) have no upstream equivalent
# at all, so checking whether they're "still present upstream" would be
# meaningless -- the missing-tool check below is scoped to CURATED_TOOLS for
# exactly this reason, not the full CURATED_TOOLS | WRAPPER_OWNED_TOOLS union.


@dataclass(frozen=True)
class UpstreamInventory:
    tool_names: frozenset[str]
    toolset_tags: frozenset[str]


def fetch_upstream_jira_server(dest: Path) -> Path:
    """Shallow-clones upstream's ``main`` into ``dest`` and returns the path
    to its Jira tool-definition module."""
    subprocess.run(
        ["git", "clone", "--depth", "1", UPSTREAM_REPO, str(dest)],
        check=True,
        capture_output=True,
    )
    path = dest / UPSTREAM_JIRA_SERVER_PATH
    if not path.is_file():
        raise FileNotFoundError(f"expected {UPSTREAM_JIRA_SERVER_PATH} in a fresh clone of {UPSTREAM_REPO}")
    return path


def parse_upstream_inventory(jira_server_path: Path) -> UpstreamInventory:
    """AST-parses ``@jira_mcp.tool(...)``-decorated functions: each yields a
    wire name ``jira_<function name>`` (respecting an explicit ``name=``
    keyword override) and its ``toolset:...`` tags."""
    tree = ast.parse(jira_server_path.read_text())
    tool_names: set[str] = set()
    toolset_tags: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            # A bare `@jira_mcp.tool` (no call, no parens) is also a valid
            # decoration -- only a `Call` decorator can carry `name=`/`tags=`
            # keywords, but the function is still a tool either way.
            if isinstance(decorator, ast.Attribute) and decorator.attr == "tool":
                tool_names.add(f"jira_{node.name}")
                continue
            if not (isinstance(decorator, ast.Call) and getattr(decorator.func, "attr", None) == "tool"):
                continue
            wire_name = node.name
            for keyword in decorator.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    wire_name = str(keyword.value.value)
                if keyword.arg == "tags" and isinstance(keyword.value, ast.Set):
                    toolset_tags |= {
                        elt.value
                        for elt in keyword.value.elts
                        if isinstance(elt, ast.Constant)
                        and isinstance(elt.value, str)
                        and elt.value.startswith("toolset:")
                    }
            tool_names.add(f"jira_{wire_name}")

    return UpstreamInventory(tool_names=frozenset(tool_names), toolset_tags=frozenset(toolset_tags))


def build_report(upstream: UpstreamInventory) -> str:
    missing_curated = sorted(CURATED_TOOLS - upstream.tool_names)
    new_tools = sorted(upstream.tool_names - CURATED_TOOLS - WRAPPER_OWNED_TOOLS)
    new_toolset_tags = sorted(upstream.toolset_tags - BASELINE_TOOLSET_TAGS)
    gone_toolset_tags = sorted(BASELINE_TOOLSET_TAGS - upstream.toolset_tags)

    lines = [
        "# upstream mcp-atlassian tool inventory",
        "",
        f"Upstream tools observed: {len(upstream.tool_names)}. "
        f"Toolset tags observed: {len(upstream.toolset_tags)}.",
        "",
    ]

    if missing_curated:
        lines += [
            "## Curated/shadowed tool missing upstream (HIGH — a mirrored tool may now 404)",
            "",
            *(f"- `{name}`" for name in missing_curated),
            "",
        ]
    else:
        lines += ["## Curated/shadowed tools: all present upstream. No action needed.", ""]

    if new_toolset_tags:
        lines += [
            "## New toolset tag(s) since the last recorded baseline",
            "",
            *(f"- `{tag}`" for tag in new_toolset_tags),
            "",
            "Consider whether any tool in a new toolset belongs in `CURATED_TOOLS`.",
            "",
        ]

    if gone_toolset_tags:
        lines += [
            "## Baseline toolset tag(s) no longer seen upstream",
            "",
            *(f"- `{tag}`" for tag in gone_toolset_tags),
            "",
        ]

    if new_tools:
        lines += [
            f"## Uncurated upstream tools not in CURATED_TOOLS/WRAPPER_OWNED_TOOLS ({len(new_tools)})",
            "",
            "Expected and informational — curation is intentionally a subset. "
            'Already reachable under `toolset_preset = "all"` with no code change.',
            "",
            *(f"- `{name}`" for name in new_tools),
            "",
        ]

    return "\n".join(lines)


def has_drift(upstream: UpstreamInventory) -> bool:
    missing_curated = CURATED_TOOLS - upstream.tool_names
    new_toolset_tags = upstream.toolset_tags - BASELINE_TOOLSET_TAGS
    gone_toolset_tags = BASELINE_TOOLSET_TAGS - upstream.toolset_tags
    return bool(missing_curated or new_toolset_tags or gone_toolset_tags)


def _gh(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def sync_github_issue(report: str) -> None:
    """Creates the tracking issue if none is open with ``ISSUE_TITLE``,
    otherwise appends the latest report as a comment on the existing one
    (idempotent: never opens a second issue for the same drift)."""
    search = _gh(
        "issue",
        "list",
        "--state",
        "open",
        "--search",
        f'"{ISSUE_TITLE}" in:title',
        "--json",
        "number,title",
    )
    if search.returncode != 0:
        raise RuntimeError(f"gh issue list failed: {search.stderr.strip()}")

    matches = [item for item in json.loads(search.stdout or "[]") if item["title"] == ISSUE_TITLE]

    if matches:
        number = matches[0]["number"]
        result = _gh("issue", "comment", str(number), "--body", report)
        action = f"updated existing issue #{number}"
    else:
        result = _gh("issue", "create", "--title", ISSUE_TITLE, "--body", report)
        action = "created a new issue"

    if result.returncode != 0:
        raise RuntimeError(f"gh issue {action} failed: {result.stderr.strip()}")
    sys.stdout.write(f"{action}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--create-issue",
        action="store_true",
        help="On drift, create-or-update the GitHub tracking issue via `gh` (requires auth).",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="mcp-atlassian-") as tmp:
        jira_server_path = fetch_upstream_jira_server(Path(tmp) / "mcp-atlassian")
        upstream = parse_upstream_inventory(jira_server_path)

    report = build_report(upstream)
    sys.stdout.write(report + "\n")
    drift = has_drift(upstream)
    sys.stdout.write(f"\ndrift detected: {drift}\n")

    if drift and args.create_issue:
        sync_github_issue(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
