"""Direct Jira Cloud REST v3 client: attachments (list, download-to-disk,
upload) and comment deletion.

Implemented directly against the REST API (not through an upstream child)
because upstream ``mcp-atlassian`` has no delete-comment tool at all, and
only exposes a base64-in-band download tool for attachments -- see
``tools_meta.WRAPPER_OWNED_TOOLS``. Cloud only in this version: Server/Data
Center endpoints differ and are out of scope (the plan's v1 decision), so
every method refuses on a ``personal_token`` site.
"""

from __future__ import annotations

import errno
import logging
import mimetypes
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import httpx
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.model import Defaults, SiteConfig
from mcp_atlassian_better.tools_meta import COMMENT_ID_RE, ISSUE_KEY_RE

_logger = logging.getLogger(__name__)

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# os.link(part, dest) errno's that mean "this filesystem/pair of paths
# doesn't support hard links at all" (as opposed to EEXIST, which means
# `dest` is simply already there) -- e.g. `part` and `dest` on different
# filesystems, or a filesystem/OS that never supports hard links. Falls back
# to a plain copy instead of leaking the raw OSError and aborting the batch.
_LINK_UNSUPPORTED_ERRNOS = frozenset({errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP})


class _AttachmentTooLarge(Exception):
    """Internal signal that a streamed download exceeded ``max_bytes``;
    always caught inside ``_download_one`` and turned into a "failed" entry,
    never propagated to a caller."""


@dataclass(frozen=True)
class AttachmentMeta:
    id: str
    filename: str
    size: int
    mime_type: str
    created: str
    author: str
    content_url: str


@dataclass(frozen=True)
class DownloadedFile:
    """``filename`` is the name actually written under ``target_dir`` -- it
    may differ from ``original_filename`` when a same-named collision (either
    on disk already, or against another attachment selected in the same
    call) forced the id-suffixed fallback name."""

    attachment_id: str
    filename: str
    original_filename: str
    path: str
    size: int
    mime_type: str


class _DownloadEntry:
    """One selected attachment that was NOT written to disk (name/reason kept
    separate from ``DownloadedFile`` so a caller can bucket "skipped" --
    already exists, both fallback names taken -- from "failed" -- refused by
    the server, too large, an unsafe filename).

    ``filename`` is the real attachment filename, known whenever the entry
    describes an attachment that was actually looked up; it's ``None`` for an
    unmatched *selector* (a requested ``filenames``/``attachment_ids`` value
    with no matching attachment at all), which instead carries ``selector``
    (e.g. ``"id:999"``) -- there's no real filename to report in that case.
    """

    def __init__(
        self,
        filename: str | None,
        reason: str,
        status: Literal["skipped", "failed"],
        *,
        selector: str | None = None,
    ) -> None:
        self.filename = filename
        self.reason = reason
        self.status = status
        self.selector = selector

    def as_dict(self) -> dict[str, str]:
        result: dict[str, str] = {"reason": self.reason, "status": self.status}
        if self.filename is not None:
            result["filename"] = self.filename
        if self.selector is not None:
            result["selector"] = self.selector
        return result


def _safe_filename(filename: str) -> str:
    """Basename only (discards any directory component a hostile or odd
    server-reported filename might carry), with control characters stripped.
    Empty, ``.``, or ``..`` after that cleanup is never a usable filename."""
    name = Path(filename.strip()).name
    name = _CONTROL_CHARS_RE.sub("", name)
    if name in ("", ".", ".."):
        return ""
    return name


def _path_segment(value: str, pattern: re.Pattern[str], label: str) -> str:
    """Defense in depth: ``wrapper_tools`` already validates and normalizes
    ``issue_key``/``comment_id`` before calling into this client, but every
    method that puts one straight into a REST URL path re-checks it here too,
    so a future caller that skips the wrapper layer can never misuse this
    client to hit a different endpoint or issue."""
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}: {value!r}")
    return value


class JiraAttachmentClient:
    """One site's attachment operations, over a caller-supplied ``httpx.AsyncClient``."""

    def __init__(
        self,
        site: SiteConfig,
        http: httpx.AsyncClient,
        *,
        max_bytes: int,
        redact: Callable[[str], str],
    ) -> None:
        self._site = site
        self._http = http
        self._max_bytes = max_bytes
        self._redact = redact

    @property
    def api_base(self) -> str:
        return f"{self._site.url}/rest/api/3"

    def _redact_exc(self, exc: BaseException) -> str:
        # Some httpx transport errors (e.g. a bare `httpx.ConnectError()`)
        # stringify to "" -- without a fallback that reads as "ClassName: "
        # with nothing after the colon, which looks like the message got
        # truncated rather than never having existed.
        detail = str(exc) or "(no detail)"
        return self._redact(f"{exc.__class__.__name__}: {detail}")

    def _shape_transport_error(self, exc: httpx.HTTPError | httpx.InvalidURL, tool_name: str) -> ToolError:
        """Gives a transport-level failure (connect/read timeout, dropped
        connection, an ``httpx.InvalidURL`` a malformed path segment slipped
        past validation and produced, ...) on the LIST or UPLOAD phase the
        same ``[site=]`` shape every other error path in this module already
        has -- used by both single-shot call sites (list/upload raise this
        directly); the per-attachment DOWNLOAD phase collects failures as
        ``_DownloadEntry`` rows instead of raising, but shares ``_redact_exc``
        for the same underlying message text."""
        return ToolError(f"[site={self._site.name}] {tool_name}: {self._redact_exc(exc)}")

    def _require_cloud(self, tool_name: str) -> None:
        if self._site.personal_token is not None:
            raise ToolError(
                f"[site={self._site.name}] {tool_name}: this tool supports Jira Cloud sites "
                f"only in this version (site '{self._site.name}' is configured as Server/Data Center)"
            )

    async def list_attachments(
        self, issue_key: str, *, tool_name: str = "jira_list_attachments"
    ) -> list[AttachmentMeta]:
        self._require_cloud(tool_name)
        try:
            issue_key = _path_segment(issue_key, ISSUE_KEY_RE, "issue_key")
        except ValueError as exc:
            raise ToolError(f"[site={self._site.name}] {tool_name}: {exc}") from exc
        path = f"/issue/{issue_key}"
        try:
            response = await self._http.get(f"{self.api_base}{path}", params={"fields": "attachment"})
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise self._shape_transport_error(exc, tool_name) from exc
        await self._raise_for_status(response, "GET", path, tool_name)
        data = response.json()
        raw_attachments = (data.get("fields") or {}).get("attachment") or []
        return [self._parse_attachment(raw) for raw in raw_attachments]

    async def download(
        self,
        issue_key: str,
        target_dir: Path,
        *,
        filenames: Sequence[str] | None = None,
        attachment_ids: Sequence[str] | None = None,
        overwrite: bool = False,
    ) -> tuple[list[DownloadedFile], list[dict[str, str]]]:
        self._require_cloud("jira_download_attachments")
        if filenames is not None and len(filenames) == 0:
            raise ToolError(
                f"[site={self._site.name}] jira_download_attachments: 'filenames' is an empty list; "
                "omit the argument entirely to download every attachment"
            )
        if attachment_ids is not None and len(attachment_ids) == 0:
            raise ToolError(
                f"[site={self._site.name}] jira_download_attachments: 'attachment_ids' is an empty list; "
                "omit the argument entirely to download every attachment"
            )
        attachments = await self.list_attachments(issue_key, tool_name="jira_download_attachments")
        selected, unmatched = self._select(attachments, filenames, attachment_ids, issue_key)

        downloaded: list[DownloadedFile] = []
        entries: list[_DownloadEntry] = list(unmatched)
        if not selected:
            # Nothing to write -- in particular, a selector that matched
            # nothing must not create an otherwise-empty target_dir.
            return downloaded, [entry.as_dict() for entry in entries]

        resolved_dir = target_dir.expanduser().resolve()
        try:
            resolved_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ToolError(
                self._redact(
                    f"[site={self._site.name}] jira_download_attachments: could not create "
                    f"target_dir '{resolved_dir}': {exc.__class__.__name__}: {exc}"
                )
            ) from exc

        used_names: set[str] = set()
        for attachment in selected:
            result = await self._download_one(
                attachment, resolved_dir, overwrite=overwrite, used_names=used_names
            )
            if isinstance(result, DownloadedFile):
                downloaded.append(result)
            else:
                entries.append(result)
        return downloaded, [entry.as_dict() for entry in entries]

    async def upload(self, issue_key: str, paths: Sequence[Path]) -> list[AttachmentMeta]:
        self._require_cloud("jira_upload_attachments")
        try:
            issue_key = _path_segment(issue_key, ISSUE_KEY_RE, "issue_key")
        except ValueError as exc:
            raise ToolError(f"[site={self._site.name}] jira_upload_attachments: {exc}") from exc
        for path in paths:
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ToolError(
                    self._redact(
                        f"[site={self._site.name}] jira_upload_attachments: could not stat "
                        f"'{path}': {exc.__class__.__name__}: {exc}"
                    )
                ) from exc
            if size > self._max_bytes:
                raise ToolError(
                    f"[site={self._site.name}] jira_upload_attachments: '{path}' is {size} bytes, "
                    f"exceeds max_bytes {self._max_bytes}"
                )
        with ExitStack() as stack:
            try:
                handles = [stack.enter_context(open(path, "rb")) for path in paths]
            except OSError as exc:
                raise ToolError(
                    self._redact(
                        f"[site={self._site.name}] jira_upload_attachments: could not open "
                        f"'{exc.filename}': {exc.__class__.__name__}: {exc}"
                    )
                ) from exc
            files = [
                ("file", (path.name, fh, mimetypes.guess_type(path.name)[0] or "application/octet-stream"))
                for path, fh in zip(paths, handles, strict=True)
            ]
            path_str = f"/issue/{issue_key}/attachments"
            try:
                response = await self._http.post(
                    f"{self.api_base}{path_str}",
                    files=files,
                    headers={"X-Atlassian-Token": "no-check"},
                )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise self._shape_transport_error(exc, "jira_upload_attachments") from exc
        await self._raise_for_status(response, "POST", path_str, "jira_upload_attachments")
        return [self._parse_attachment(raw) for raw in response.json()]

    async def delete_comment(self, issue_key: str, comment_id: str) -> None:
        """Permanently deletes one comment. ``issue_key``/``comment_id`` are
        validated by the caller (``wrapper_tools._validate_issue_key`` /
        ``_validate_comment_id``) before either ever reaches this method --
        the ``_path_segment`` checks below are a defense-in-depth belt for a
        future caller that skips that wrapper layer, not the primary guard."""
        self._require_cloud("jira_delete_comment")
        try:
            issue_key = _path_segment(issue_key, ISSUE_KEY_RE, "issue_key")
            comment_id = _path_segment(comment_id, COMMENT_ID_RE, "comment_id")
        except ValueError as exc:
            raise ToolError(f"[site={self._site.name}] jira_delete_comment: {exc}") from exc
        path = f"/issue/{issue_key}/comment/{comment_id}"
        try:
            response = await self._http.delete(f"{self.api_base}{path}")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise self._shape_transport_error(exc, "jira_delete_comment") from exc
        await self._raise_for_status(response, "DELETE", path, "jira_delete_comment")

    def _select(
        self,
        attachments: Sequence[AttachmentMeta],
        filenames: Sequence[str] | None,
        attachment_ids: Sequence[str] | None,
        issue_key: str,
    ) -> tuple[list[AttachmentMeta], list[_DownloadEntry]]:
        if filenames is None and attachment_ids is None:
            return list(attachments), []
        filename_set = set(filenames) if filenames is not None else set()
        id_set = set(attachment_ids) if attachment_ids is not None else set()
        selected = [a for a in attachments if a.filename in filename_set or a.id in id_set]

        matched_filenames = {a.filename for a in selected}
        matched_ids = {a.id for a in selected}
        unmatched = [
            _DownloadEntry(name, f"no attachment with filename '{name}' on {issue_key}", "failed")
            for name in sorted(filename_set - matched_filenames)
        ] + [
            # No real filename to report for an unmatched id -- `selector`
            # carries it instead (see `_DownloadEntry`'s docstring).
            _DownloadEntry(
                None,
                f"no attachment with id '{att_id}' on {issue_key}",
                "failed",
                selector=f"id:{att_id}",
            )
            for att_id in sorted(id_set - matched_ids)
        ]
        return selected, unmatched

    def _pick_dest_name(
        self, safe: str, attachment_id: str, target_dir: Path, used_names: set[str], *, overwrite: bool
    ) -> tuple[str | None, str | None]:
        """Chooses the basename this attachment writes to, or ``(None,
        reason)`` if both it and its id-suffixed fallback are already taken.

        ``used_names`` (names already claimed by an earlier attachment in
        THIS SAME ``download()`` call) always forces the fallback name,
        regardless of ``overwrite`` -- ``overwrite`` is about a file that
        pre-dates this call, not about two attachments selected together
        that happen to share a name, which must never collide with each
        other either way. The returned reason (when blocked) distinguishes
        which of those two situations blocked each candidate name, rather
        than a single message that can't tell a caller whether a re-run with
        a clean directory would behave differently.
        """
        if safe not in used_names and (overwrite or not (target_dir / safe).exists()):
            return safe, None
        safe_reason = "claimed earlier in this same call" if safe in used_names else "already on disk"
        stem, suffix = Path(safe).stem, Path(safe).suffix
        candidate = f"{stem}-{attachment_id}{suffix}"
        if candidate in used_names:
            return None, (
                f"'{safe}' ({safe_reason}) and '{candidate}' (claimed earlier in this "
                "same call) are both already taken"
            )
        if not overwrite and (target_dir / candidate).exists():
            return None, (
                f"'{safe}' ({safe_reason}) and '{candidate}' (already on disk) are both already taken"
            )
        return candidate, None

    async def _download_one(
        self, attachment: AttachmentMeta, target_dir: Path, *, overwrite: bool, used_names: set[str]
    ) -> DownloadedFile | _DownloadEntry:
        safe = _safe_filename(attachment.filename)
        if not safe:
            return _DownloadEntry(attachment.filename, "unsafe or empty filename", "failed")
        if not attachment.content_url:
            return _DownloadEntry(attachment.filename, "attachment has no content URL", "failed")

        dest_name, blocked_reason = self._pick_dest_name(
            safe, attachment.id, target_dir, used_names, overwrite=overwrite
        )
        if dest_name is None:
            assert blocked_reason is not None
            return _DownloadEntry(attachment.filename, blocked_reason, "skipped")
        dest = target_dir / dest_name

        try:
            result = await self._stream_to_dest(attachment, dest, target_dir, overwrite=overwrite)
        except (OSError, httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # Belt: any per-file failure `_stream_to_dest`'s own steps didn't
            # already shape into a clearer `_DownloadEntry` lands here instead
            # of escaping `download()`'s loop and aborting every other
            # attachment selected in the same call (the exact bug an
            # unshaped `IsADirectoryError` from `os.replace` caused).
            return _DownloadEntry(attachment.filename, self._redact_exc(exc), "failed")
        if isinstance(result, DownloadedFile):
            used_names.add(result.filename)
        return result

    async def _stream_to_dest(
        self, attachment: AttachmentMeta, dest: Path, target_dir: Path, *, overwrite: bool
    ) -> DownloadedFile | _DownloadEntry:
        # A unique scratch file per CALL, not per attachment id: two
        # concurrent `download()` calls selecting the SAME attachment id (or
        # simply targeting the same `dest`) must never share one `.part`
        # name, or one call's cleanup can unlink the file the other is still
        # streaming into. `mkstemp` guarantees this (O_CREAT|O_EXCL under the
        # hood) even when both calls race on the exact same `dest.name`.
        try:
            fd, part_name = tempfile.mkstemp(dir=str(target_dir), prefix=f".{dest.name}.", suffix=".part")
        except OSError as exc:
            return _DownloadEntry(
                attachment.filename, f"could not create temp file: {exc.__class__.__name__}: {exc}", "failed"
            )
        part = Path(part_name)

        # `handle` takes ownership of `fd` from this line on: every exit
        # below -- a shaped `_DownloadEntry`, an `httpx.HTTPError`, or a
        # `BaseException` (e.g. task cancellation) -- runs through the
        # `finally` blocks, so the fd is always closed and `part` is always
        # removed unless `_finalize` actually publishes it. Previously
        # `os.fdopen(fd, ...)` didn't happen until deep inside the response
        # handling, so a non-2xx status, an over-cap `Content-Length`, or an
        # error raised just setting up the stream left the raw `mkstemp` fd
        # open with nothing left to close it.
        try:
            handle = os.fdopen(fd, "wb")
        except OSError as exc:
            os.close(fd)
            part.unlink(missing_ok=True)
            return _DownloadEntry(
                attachment.filename, f"could not open temp file: {exc.__class__.__name__}: {exc}", "failed"
            )

        published = False
        try:
            written = 0
            try:
                # Cloud's attachment `content` URL 302s to a pre-signed
                # media-CDN URL on a different host; httpx drops the
                # Authorization header on that cross-host redirect. That's
                # correct, not a bug -- the redirect target is pre-signed and
                # needs no auth of ours. Never "fix" this into replaying our
                # Basic auth cross-host, which would leak it to whatever host
                # Jira sends us to.
                async with self._http.stream(
                    "GET", attachment.content_url, follow_redirects=True
                ) as response:
                    try:
                        await self._raise_for_status(
                            response, "GET", attachment.content_url, "jira_download_attachments"
                        )
                    except ToolError as exc:
                        return _DownloadEntry(attachment.filename, str(exc), "failed")

                    declared_size = self._parse_content_length(response.headers.get("content-length"))
                    if declared_size is not None and declared_size > self._max_bytes:
                        return _DownloadEntry(
                            attachment.filename,
                            f"reported size {declared_size} bytes exceeds max_bytes {self._max_bytes}",
                            "failed",
                        )

                    try:
                        async for chunk in response.aiter_bytes(65536):
                            written += len(chunk)
                            if written > self._max_bytes:
                                raise _AttachmentTooLarge
                            handle.write(chunk)
                    except _AttachmentTooLarge:
                        return _DownloadEntry(
                            attachment.filename,
                            f"exceeded max_bytes {self._max_bytes} while streaming",
                            "failed",
                        )
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                # A transport-level failure (connect/read timeout, dropped
                # connection, a mid-stream ReadError, ...) or an error raised
                # while just setting up the stream, before a single byte was
                # ever written -- not a Jira-returned error status. `dest`
                # was never touched either way.
                return _DownloadEntry(attachment.filename, self._redact_exc(exc), "failed")
            finally:
                handle.close()

            result = self._finalize(attachment, part, dest, overwrite=overwrite, written=written)
            if isinstance(result, DownloadedFile):
                published = True
            return result
        finally:
            if not published:
                part.unlink(missing_ok=True)

    def _finalize(
        self, attachment: AttachmentMeta, part: Path, dest: Path, *, overwrite: bool, written: int
    ) -> DownloadedFile | _DownloadEntry:
        """Publishes the fully-streamed ``part`` scratch file to ``dest``.

        ``part`` is unlinked in every case: ``os.replace`` already consumes
        its name, ``os.link`` leaves it behind as a redundant second name to
        the same content, and the copy fallback below removes it explicitly
        too -- either way, nothing should ever be left under a `.part` name
        once this returns.
        """
        try:
            if overwrite:
                try:
                    os.replace(part, dest)
                except IsADirectoryError:
                    return _DownloadEntry(attachment.filename, "destination is a directory", "failed")
            else:
                try:
                    os.link(part, dest)
                except FileExistsError:
                    return _DownloadEntry(attachment.filename, f"'{dest.name}' already exists", "skipped")
                except OSError as exc:
                    if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                        raise
                    try:
                        self._copy_part_to_dest(part, dest)
                    except FileExistsError:
                        return _DownloadEntry(attachment.filename, f"'{dest.name}' already exists", "skipped")
        except OSError as exc:
            return _DownloadEntry(
                attachment.filename,
                f"could not finalize download: {self._redact_exc(exc)}",
                "failed",
            )
        finally:
            part.unlink(missing_ok=True)

        return DownloadedFile(
            attachment_id=attachment.id,
            filename=dest.name,
            original_filename=attachment.filename,
            path=str(dest),
            size=written,
            mime_type=attachment.mime_type,
        )

    @staticmethod
    def _copy_part_to_dest(part: Path, dest: Path) -> None:
        """Used when ``_finalize``'s own ``os.link(part, dest)`` can't
        publish the download at all -- in practice a filesystem/OS that
        doesn't support hard links (``EPERM``/``ENOTSUP``/``EOPNOTSUPP``).
        ``EXDEV`` (cross-device) is in ``_LINK_UNSUPPORTED_ERRNOS``
        defensively, but every path this method ever links -- ``part``, this
        method's own ``.copy`` temp, and ``dest`` -- lives in ``target_dir``
        (see ``_stream_to_dest``'s and this method's own ``tempfile.mkstemp``
        calls), so cross-device is not actually reachable here.

        Copies through a second temp file in ``dest``'s own directory, then
        tries to publish THAT via ``os.link`` too. If that also fails, this
        falls all the way back to writing ``dest`` directly -- still
        ``O_EXCL``, so a same-named file that appeared on disk after
        ``_pick_dest_name`` checked (but before this runs) still isn't
        silently clobbered, raising ``FileExistsError`` the same as the
        ordinary ``os.link`` collision case. The actual protection against a
        truncated ``dest`` on THIS direct-write path is the
        ``except BaseException: dest.unlink(...); raise`` below, not the
        ``os.link`` publish step above it (that step's atomicity is what
        protects the copy-then-link path, not this one) -- a failure partway
        through the direct write removes ``dest`` again rather than leaving
        a partial download under the delivered filename.

        ``tempfile.mkstemp`` creates its file with mode 0o600; the O_EXCL
        fallback below matches that explicitly, since plain ``os.open``
        would otherwise default to 0o777 (0o755 after umask).
        """
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".copy")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as tmp_handle, open(part, "rb") as part_handle:
                shutil.copyfileobj(part_handle, tmp_handle)
            try:
                os.link(tmp, dest)
            except OSError as exc:
                if exc.errno not in _LINK_UNSUPPORTED_ERRNOS:
                    raise
                dfd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(dfd, "wb") as dest_handle, open(tmp, "rb") as tmp_handle2:
                        shutil.copyfileobj(tmp_handle2, dest_handle)
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _parse_content_length(raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            # A malformed Content-Length is treated as absent rather than
            # crashing the download -- the streamed byte count still caps it.
            return None

    def _parse_attachment(self, raw: dict[str, Any]) -> AttachmentMeta:
        author = raw.get("author") or {}
        return AttachmentMeta(
            id=str(raw["id"]),
            filename=raw["filename"],
            size=int(raw.get("size", 0)),
            mime_type=raw.get("mimeType", "application/octet-stream"),
            created=raw.get("created", ""),
            author=author.get("displayName", ""),
            content_url=raw.get("content", ""),
        )

    async def _raise_for_status(
        self, response: httpx.Response, method: str, path: str, tool_name: str
    ) -> None:
        if response.is_success:
            return
        if not response.is_stream_consumed:
            await response.aread()
        detail = self._error_detail(response)
        message = self._redact(
            f"[site={self._site.name}] {tool_name}: Jira returned {response.status_code} "
            f"for {method} {path}: {detail}"
        )
        raise ToolError(message)

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.reason_phrase or str(response.status_code)
        if isinstance(body, dict):
            messages = [str(m) for m in (body.get("errorMessages") or [])]
            errors = body.get("errors")
            if isinstance(errors, dict):
                messages.extend(f"{key}: {value}" for key, value in errors.items())
            if messages:
                return "; ".join(messages)
        return response.reason_phrase or str(response.status_code)


class AttachmentClientRegistry:
    """One lazily-created ``httpx.AsyncClient`` (and ``JiraAttachmentClient``)
    per configured site, mirroring ``ChildManager``'s per-site lifecycle so
    ``server.py`` can close everything together on shutdown."""

    def __init__(
        self, sites: Sequence[SiteConfig], defaults: Defaults, *, redact: Callable[[str], str]
    ) -> None:
        self._sites = {site.name: site for site in sites}
        self._defaults = defaults
        self._redact = redact
        self._http_clients: dict[str, httpx.AsyncClient] = {}
        self._clients: dict[str, JiraAttachmentClient] = {}

    def get(self, site_name: str) -> JiraAttachmentClient:
        client = self._clients.get(site_name)
        if client is not None:
            return client
        site = self._sites[site_name]
        http = self._build_http_client(site)
        self._http_clients[site_name] = http
        client = JiraAttachmentClient(
            site, http, max_bytes=self._defaults.attachment_max_bytes, redact=self._redact
        )
        self._clients[site_name] = client
        return client

    def _build_http_client(self, site: SiteConfig) -> httpx.AsyncClient:
        timeout = httpx.Timeout(
            self._defaults.call_timeout_seconds, connect=self._defaults.connect_timeout_seconds
        )
        auth = (
            httpx.BasicAuth(site.username or "", site.api_token.get_secret_value())
            if site.api_token is not None
            else None
        )
        return httpx.AsyncClient(timeout=timeout, auth=auth)

    async def aclose(self) -> None:
        for site_name, http in self._http_clients.items():
            try:
                await http.aclose()
            except Exception as exc:  # noqa: BLE001 - one site's teardown must not block the rest
                _logger.warning(
                    "error closing attachment client for '%s': %s", site_name, self._redact(str(exc))
                )
