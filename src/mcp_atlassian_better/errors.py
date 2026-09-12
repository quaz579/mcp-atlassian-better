"""Exception hierarchy for mcp-atlassian-better."""

from __future__ import annotations


class McpAtlassianBetterError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(McpAtlassianBetterError):
    """The configuration file or environment overlay is invalid or incomplete."""


class SiteResolutionError(McpAtlassianBetterError, ValueError):
    """A tool call's target site could not be determined unambiguously.

    Subclasses ValueError too so a caller that only catches ValueError (the
    common shape for "bad tool arguments") still catches these.
    """


class UnknownSiteError(SiteResolutionError):
    """An explicit ``site`` argument does not match any configured site."""


class AmbiguousSiteError(SiteResolutionError):
    """No configured site could be inferred from the call's arguments."""


class UnknownPrefixError(SiteResolutionError):
    """An issue or project key referenced a prefix no site is configured for."""


class CrossSiteError(SiteResolutionError):
    """A single tool call's arguments referenced more than one configured site."""


class SchemaConflictError(McpAtlassianBetterError):
    """An upstream tool's input schema already defines the ``site`` property."""
