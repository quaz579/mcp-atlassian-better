"""Disk-safety behavior of ``JiraAttachmentClient.download``: no clobbering,
basename-only filenames, size caps enforced both from ``Content-Length`` and
the actual streamed byte count, and directory/overwrite handling."""

from __future__ import annotations

import errno
import os
import shutil
import stat
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from mcp_atlassian_better.attachments import DownloadedFile, JiraAttachmentClient
from mcp_atlassian_better.model import SiteConfig
from mcp_atlassian_better.secrets import Secret

_ISSUE_URL = "https://acme.atlassian.net/rest/api/3/issue/ACME-1"


def _open_fd_count() -> int:
    """Portable-enough open-fd count for a leak-delta assertion: `/dev/fd` on
    macOS, `/proc/self/fd` on Linux (where `/dev/fd` may not exist)."""
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return len(os.listdir("/proc/self/fd"))


def _site() -> SiteConfig:
    return SiteConfig(
        name="acme",
        url="https://acme.atlassian.net",
        key_prefixes=("ACME",),
        username="you@example.com",
        api_token=Secret("token"),
    )


def _attachment(att_id: str, filename: str) -> dict[str, object]:
    return {
        "id": att_id,
        "filename": filename,
        "size": 0,
        "mimeType": "text/plain",
        "created": "2026-09-10T12:00:00.000+0000",
        "author": {"displayName": "Example User"},
        "content": f"https://acme.atlassian.net/rest/api/3/attachment/content/{att_id}",
    }


def _mock_list(attachments: list[dict[str, object]]) -> None:
    respx.get(_ISSUE_URL, params={"fields": "attachment"}).mock(
        return_value=httpx.Response(200, json={"fields": {"attachment": attachments}})
    )


def _mock_content(att_id: str, body: bytes, *, chunked: bool = False) -> None:
    url = f"https://acme.atlassian.net/rest/api/3/attachment/content/{att_id}"
    if chunked:

        async def gen() -> AsyncIterator[bytes]:
            step = 1024
            for i in range(0, len(body), step):
                yield body[i : i + step]

        respx.get(url).mock(return_value=httpx.Response(200, content=gen()))
    else:
        respx.get(url).mock(return_value=httpx.Response(200, content=body))


@pytest.fixture
async def http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(auth=httpx.BasicAuth("you@example.com", "token")) as client:
        yield client


def _client(http_client: httpx.AsyncClient, *, max_bytes: int = 10_000_000) -> JiraAttachmentClient:
    return JiraAttachmentClient(_site(), http_client, max_bytes=max_bytes, redact=lambda t: t)


@respx.mock
async def test_existing_file_falls_back_then_skips_when_both_taken(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_bytes(b"original")
    (tmp_path / "notes-1.txt").write_bytes(b"fallback-taken")
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"new-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "skipped"
    assert (tmp_path / "notes.txt").read_bytes() == b"original"
    assert (tmp_path / "notes-1.txt").read_bytes() == b"fallback-taken"


@respx.mock
async def test_existing_file_falls_back_to_id_suffixed_name(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_bytes(b"original")
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"new-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert entries == []
    assert len(downloaded) == 1
    assert downloaded[0].path == str(tmp_path / "notes-1.txt")
    assert (tmp_path / "notes.txt").read_bytes() == b"original"
    assert (tmp_path / "notes-1.txt").read_bytes() == b"new-content"


@respx.mock
async def test_path_traversal_filename_becomes_basename(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "../../etc/passwd")])
    _mock_content("1", b"payload")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert entries == []
    assert len(downloaded) == 1
    result_path = Path(downloaded[0].path)
    assert result_path.parent == tmp_path
    assert result_path.name == "passwd"


@respx.mock
async def test_two_same_named_attachments_both_land(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    _mock_list([_attachment("1", "notes.txt"), _attachment("2", "notes.txt")])
    _mock_content("1", b"first")
    _mock_content("2", b"second")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert entries == []
    assert len(downloaded) == 2
    paths = {Path(d.path).name: Path(d.path).read_bytes() for d in downloaded}
    assert paths["notes.txt"] == b"first"
    assert paths["notes-2.txt"] == b"second"


@respx.mock
async def test_content_length_over_max_bytes_aborts_before_writing(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "big.bin")])
    respx.get("https://acme.atlassian.net/rest/api/3/attachment/content/1").mock(
        return_value=httpx.Response(200, content=b"x" * 20, headers={"content-length": "999999"})
    )

    before = _open_fd_count()
    downloaded, entries = await _client(http_client, max_bytes=100).download("ACME-1", tmp_path)
    after = _open_fd_count()

    assert downloaded == []
    assert entries[0]["status"] == "failed"
    assert "max_bytes" in entries[0]["reason"]
    assert not (tmp_path / "big.bin").exists()
    assert list(tmp_path.iterdir()) == []  # no leftover .part scratch file
    assert after == before  # the mkstemp fd was closed, not just orphaned


@respx.mock
async def test_streamed_body_exceeding_max_bytes_with_no_content_length_leaves_no_partial_file(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "big.bin")])
    _mock_content("1", b"x" * 5000, chunked=True)

    downloaded, entries = await _client(http_client, max_bytes=100).download("ACME-1", tmp_path)

    assert downloaded == []
    assert entries[0]["status"] == "failed"
    assert "max_bytes" in entries[0]["reason"]
    assert "while streaming" in entries[0]["reason"]
    assert not (tmp_path / "big.bin").exists()
    assert list(tmp_path.iterdir()) == []  # no leftover .part scratch file either


@respx.mock
async def test_missing_target_dir_is_created(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"hello")
    target = tmp_path / "nested" / "does" / "not" / "exist"

    downloaded, entries = await _client(http_client).download("ACME-1", target)

    assert entries == []
    assert len(downloaded) == 1
    assert Path(downloaded[0].path).read_bytes() == b"hello"


@respx.mock
async def test_overwrite_true_replaces_existing_file(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes(b"stale")
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"fresh")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, overwrite=True)

    assert entries == []
    assert len(downloaded) == 1
    assert downloaded[0].path == str(tmp_path / "notes.txt")
    assert (tmp_path / "notes.txt").read_bytes() == b"fresh"


@respx.mock
async def test_filenames_selection_downloads_only_the_named_attachment(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "a.txt"), _attachment("2", "b.txt")])
    _mock_content("1", b"aaa")
    _mock_content("2", b"bbb")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, filenames=["b.txt"])

    assert entries == []
    assert [d.filename for d in downloaded] == ["b.txt"]


@respx.mock
async def test_attachment_ids_selection_downloads_only_the_matching_id(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "a.txt"), _attachment("2", "b.txt")])
    _mock_content("1", b"aaa")
    _mock_content("2", b"bbb")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, attachment_ids=["1"])

    assert entries == []
    assert [d.filename for d in downloaded] == ["a.txt"]


@respx.mock
@pytest.mark.parametrize("bad_name", ["", ".", "..", "\x00\x01\x02"])
async def test_unsafe_filename_is_a_failed_entry_not_a_crash(
    http_client: httpx.AsyncClient, tmp_path: Path, bad_name: str
) -> None:
    _mock_list([_attachment("1", bad_name)])

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert entries[0]["reason"] == "unsafe or empty filename"


@respx.mock
async def test_intra_batch_same_name_with_overwrite_lands_both_files(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "notes.txt"), _attachment("2", "notes.txt")])
    _mock_content("1", b"first")
    _mock_content("2", b"second")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, overwrite=True)

    assert entries == []
    assert len(downloaded) == 2
    paths = {Path(d.path) for d in downloaded}
    assert len(paths) == 2  # two distinct files, not one overwriting the other
    contents = {path.read_bytes() for path in paths}
    assert contents == {b"first", b"second"}


@respx.mock
async def test_mid_stream_failure_with_overwrite_leaves_original_file_intact(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    (tmp_path / "notes.txt").write_bytes(b"original-untouched")
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"x" * 5000, chunked=True)

    downloaded, entries = await _client(http_client, max_bytes=100).download(
        "ACME-1", tmp_path, overwrite=True
    )

    assert downloaded == []
    assert entries[0]["status"] == "failed"
    assert (tmp_path / "notes.txt").read_bytes() == b"original-untouched"
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]  # no leftover .part


@respx.mock
async def test_unmatched_attachment_id_is_a_failed_entry_and_creates_no_directory(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _mock_list([_attachment("1", "a.txt")])
    target = tmp_path / "fresh-nonexistent-dir"

    downloaded, entries = await _client(http_client).download("ACME-1", target, attachment_ids=["999"])

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert "999" in entries[0]["reason"]
    assert "ACME-1" in entries[0]["reason"]
    assert not target.exists()


@respx.mock
async def test_unmatched_filename_is_a_failed_entry(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    _mock_list([_attachment("1", "a.txt")])

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, filenames=["nope.txt"])

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert "nope.txt" in entries[0]["reason"]


async def test_unmatched_id_entry_carries_a_selector_not_a_filename(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    with respx.mock:
        _mock_list([_attachment("1", "a.txt")])
        downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, attachment_ids=["999"])

    assert downloaded == []
    assert entries == [
        {"selector": "id:999", "reason": "no attachment with id '999' on ACME-1", "status": "failed"}
    ]


async def test_empty_filenames_list_is_refused(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="filenames.*empty"):
        await _client(http_client).download("ACME-1", tmp_path, filenames=[])


async def test_empty_attachment_ids_list_is_refused(http_client: httpx.AsyncClient, tmp_path: Path) -> None:
    with pytest.raises(ToolError, match="attachment_ids.*empty"):
        await _client(http_client).download("ACME-1", tmp_path, attachment_ids=[])


@respx.mock
async def test_overwrite_onto_a_directory_is_a_failed_entry_not_a_crash(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Regression: `os.replace(part, dest)` when `dest` is an existing
    directory raises an unshaped `IsADirectoryError` that used to escape
    `_download_one` entirely and abort the whole batch."""
    (tmp_path / "notes.txt").mkdir()
    _mock_list([_attachment("1", "notes.txt"), _attachment("2", "other.txt")])
    _mock_content("1", b"new-content")
    _mock_content("2", b"other-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path, overwrite=True)

    assert [d.filename for d in downloaded] == ["other.txt"]
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert entries[0]["reason"] == "destination is a directory"
    assert (tmp_path / "notes.txt").is_dir()
    # No leftover `.part` scratch file for either attachment.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["notes.txt", "other.txt"]


async def test_concurrent_downloads_of_the_same_attachment_id_do_not_clobber_each_other(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Regression: two `download()` calls selecting the same attachment id
    into the same target_dir used to share one `.part` name derived only
    from `dest.name` and the attachment id -- one call's cleanup could unlink
    the file the other call was still streaming into. `mkstemp` gives each
    call's scratch file a unique name regardless, proved here by running two
    downloads truly concurrently (staggered so their streaming overlaps) in
    the same task group."""

    def make_response(request: httpx.Request) -> httpx.Response:
        async def body() -> AsyncIterator[bytes]:
            yield b"first-chunk-"
            await anyio.sleep(0.05)
            yield b"second-chunk"

        return httpx.Response(200, content=body())

    with respx.mock:
        _mock_list([_attachment("1", "notes.txt")])
        respx.get("https://acme.atlassian.net/rest/api/3/attachment/content/1").mock(
            side_effect=make_response
        )

        client = _client(http_client)
        results: list[tuple[list[DownloadedFile], list[dict[str, str]]]] = []

        async def run() -> None:
            results.append(await client.download("ACME-1", tmp_path, overwrite=True))

        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            tg.start_soon(run)

    assert len(results) == 2
    for downloaded, entries in results:
        assert entries == []
        assert len(downloaded) == 1
    assert (tmp_path / "notes.txt").read_bytes() == b"first-chunk-second-chunk"
    # No leftover `.part` scratch file from either call.
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]


@respx.mock
async def test_mid_stream_read_error_is_a_shaped_failed_entry_with_no_partial_file(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    async def broken_body() -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx.ReadError("connection reset mid-stream")

    _mock_list([_attachment("1", "notes.txt")])
    respx.get("https://acme.atlassian.net/rest/api/3/attachment/content/1").mock(
        return_value=httpx.Response(200, content=broken_body())
    )

    before = _open_fd_count()
    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)
    after = _open_fd_count()

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert "ReadError" in entries[0]["reason"]
    assert list(tmp_path.iterdir()) == []  # no partial file, no leftover .part
    assert after == before  # the mkstemp fd was closed, not just orphaned


@respx.mock
async def test_repeated_404_downloads_leak_no_file_descriptors(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A non-2xx content response must close (not just orphan) the `mkstemp`
    fd -- regression coverage for the fd that used to stay open on every
    early exit between `mkstemp` and the old, deeply-nested `os.fdopen`."""
    attachments = [_attachment(str(i), f"missing-{i}.bin") for i in range(30)]
    _mock_list(attachments)
    for i in range(30):
        respx.get(f"https://acme.atlassian.net/rest/api/3/attachment/content/{i}").mock(
            return_value=httpx.Response(404, json={"errorMessages": ["not found"]})
        )

    before = _open_fd_count()
    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)
    after = _open_fd_count()

    assert downloaded == []
    assert len(entries) == 30
    assert all(e["status"] == "failed" for e in entries)
    assert list(tmp_path.iterdir()) == []
    assert after == before


@respx.mock
async def test_base_exception_mid_stream_closes_fd_and_leaves_no_part_file(
    http_client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A `BaseException` mid-stream (e.g. task cancellation) must still be
    let through to the caller -- it must never leave the `mkstemp` fd open
    or the `.part` scratch file behind on its way out."""

    class _FakeCancellation(BaseException):
        pass

    async def cancelled_body() -> AsyncIterator[bytes]:
        yield b"partial"
        raise _FakeCancellation

    _mock_list([_attachment("1", "notes.txt")])
    respx.get("https://acme.atlassian.net/rest/api/3/attachment/content/1").mock(
        return_value=httpx.Response(200, content=cancelled_body())
    )

    before = _open_fd_count()
    with pytest.raises(_FakeCancellation):
        await _client(http_client).download("ACME-1", tmp_path)
    after = _open_fd_count()

    assert list(tmp_path.iterdir()) == []
    assert after == before


@respx.mock
async def test_no_clobber_falls_back_to_a_copy_when_hard_links_are_unsupported(
    http_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`os.link` can fail with e.g. EXDEV (different filesystems) or EPERM/
    ENOTSUP/EOPNOTSUPP (no hard-link support at all) rather than EEXIST --
    those must fall back to a plain copy instead of leaking the raw OSError."""

    def flaky_link(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", flaky_link)
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"cross-device-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert entries == []
    assert len(downloaded) == 1
    dest = tmp_path / "notes.txt"
    assert dest.read_bytes() == b"cross-device-content"
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


@respx.mock
async def test_no_clobber_copy_fallback_still_refuses_a_dest_that_appears_mid_race(
    http_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy fallback still opens `dest` with O_EXCL: if `dest` appears on
    disk between `_pick_dest_name`'s check and the fallback actually running,
    it's still not clobbered. Simulated by having the patched `os.link`
    itself create `dest` before raising -- standing in for a real race
    between that check and this call."""
    dest = tmp_path / "notes.txt"

    def racing_link(src: object, dst: object, **kwargs: object) -> None:
        Path(str(dst)).write_bytes(b"raced-in-content")
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", racing_link)
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"new-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "skipped"
    assert dest.read_bytes() == b"raced-in-content"


def test_copy_part_to_dest_direct_write_fallback_second_call_finds_dest_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whitebox on `_copy_part_to_dest` itself: exercises its OWN direct
    O_EXCL write (both `os.link` attempts -- the outer one `_finalize` tries
    first, and the inner one this method tries before falling all the way
    back -- fail with an unsupported-hard-link errno). First call: `dest`
    doesn't exist yet, so the direct write succeeds. Second call targeting
    the SAME `dest`: the O_EXCL open itself raises `FileExistsError` (not
    `_pick_dest_name`'s pre-check, which this whitebox call bypasses
    entirely) -- `dest` must be left exactly as call 1 wrote it, and the
    `.copy` scratch temp must not survive either call."""

    def flaky_link(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(os, "link", flaky_link)

    dest = tmp_path / "dest.txt"
    part1 = tmp_path / "part1"
    part1.write_bytes(b"first-content")
    JiraAttachmentClient._copy_part_to_dest(part1, dest)  # noqa: SLF001 - whitebox on our own class

    assert dest.read_bytes() == b"first-content"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dest.txt", "part1"]

    part2 = tmp_path / "part2"
    part2.write_bytes(b"second-content")
    with pytest.raises(FileExistsError):
        JiraAttachmentClient._copy_part_to_dest(part2, dest)  # noqa: SLF001 - whitebox on our own class

    assert dest.read_bytes() == b"first-content"  # untouched by the failed second call
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "dest.txt",
        "part1",
        "part2",
    ]  # no leftover .copy temp


@respx.mock
async def test_copy_fallback_mid_copy_failure_leaves_no_truncated_dest(
    http_client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure partway through the copy fallback's write (e.g. disk full)
    must never leave a truncated file under `dest`'s final name, and must
    not leak the fallback's own second temp file either."""

    def flaky_link(src: object, dst: object, **kwargs: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    def flaky_copyfileobj(fsrc: object, fdst: Any, length: int = 0) -> None:
        fdst.write(b"xx")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "link", flaky_link)
    monkeypatch.setattr(shutil, "copyfileobj", flaky_copyfileobj)
    _mock_list([_attachment("1", "notes.txt")])
    _mock_content("1", b"full-content")

    downloaded, entries = await _client(http_client).download("ACME-1", tmp_path)

    assert downloaded == []
    assert len(entries) == 1
    assert entries[0]["status"] == "failed"
    assert not (tmp_path / "notes.txt").exists()
    assert list(tmp_path.iterdir()) == []  # no truncated dest, no leftover temp
