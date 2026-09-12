"""Real-subprocess proof of the M2r2 MEDIUM finding: the shutdown signal
receiver must be armed BEFORE `start_all`, not after -- otherwise a SIGTERM
landing during the connect window hits the OS default disposition (the
process dies with no `finally` ever running) and every already-spawned child
is orphaned. Also covers the watchdog's exit-code fidelity (M2r2 item 5)."""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mcp_atlassian_better.server import SHUTDOWN_WATCHDOG_SECONDS

# The watchdog's own bound plus slack for interpreter/process-spawn overhead
# on a loaded CI runner -- NOT a re-assertion of the exact round-2 timings
# (parent 5.08s / children 0.32s), which the real run measures precisely.
_MAX_SHUTDOWN_SECONDS = SHUTDOWN_WATCHDOG_SECONDS + 5.0


def _write_hanging_upstream_script(tmp_path: Path) -> Path:
    """A fake upstream that spawns (proving `start_all` got as far as
    launching the real OS process) but never speaks MCP -- the connect
    handshake hangs until the site is torn down or the process is killed."""
    script = tmp_path / "hanging_upstream.py"
    script.write_text(
        "import os, sys, time\n"
        "if '--version' not in sys.argv:\n"
        "    with open(sys.argv[1], 'w') as f:\n"
        "        f.write(str(os.getpid()))\n"
        "time.sleep(120)\n"
    )
    return script


def _write_config(tmp_path: Path, *, command: list[str]) -> Path:
    config_path = tmp_path / "config.toml"
    command_toml = ", ".join(repr(part) for part in command)
    config_path.write_text(
        f"""
        [defaults]
        username = "you@example.com"
        api_token = "test-token"
        connect_timeout_seconds = 30
        call_timeout_seconds = 5

        [upstream]
        command = [{command_toml}]

        [[sites]]
        name = "acme"
        url = "https://acme.atlassian.net"
        key_prefixes = ["ACME"]
        """
    )
    return config_path


def _wait_for_file(path: Path, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"{path} never appeared within {timeout}s")
        time.sleep(0.02)


def _pid_alive(pid: int) -> bool:
    try:
        __import__("os").kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _cpu_seconds(pid: int) -> float:
    """Total user+system CPU time consumed by ``pid`` so far, in seconds.

    Absolute value is meaningless (interpreter startup + `import fastmcp`
    alone can cost several tenths of a second) -- only a delta across a
    window is ever compared. Prefers ``/proc`` (CI is ubuntu-only, per
    ``.github/workflows/ci.yaml``): ``ps -o time`` on Linux reports whole
    seconds, too coarse for this comparison. The macOS/BSD `ps -o time=`
    fallback is only for running this test locally.
    """
    if os.path.exists("/proc"):
        with open(f"/proc/{pid}/stat") as f:
            # `comm` (field 2) can itself contain spaces/parens; splitting
            # on the LAST ')' skips past it before counting fields.
            after_comm = f.read().rsplit(")", 1)[1].split()
        utime, stime = int(after_comm[11]), int(after_comm[12])  # fields 14, 15
        return (utime + stime) / os.sysconf("SC_CLK_TCK")
    out = subprocess.run(
        ["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=True
    ).stdout.strip()
    seconds = 0.0
    for part in out.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def test_sigterm_during_startup_does_not_orphan_the_spawned_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_hanging_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(pid_file, timeout=15.0)
        child_pid = int(pid_file.read_text().strip())
        assert _pid_alive(child_pid), "fake upstream child never actually started"

        t_signal = time.monotonic()
        proc.send_signal(signal.SIGTERM)

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of SIGTERM "
                "landing during the connect window (armed-before-start_all regression)"
            )
        parent_elapsed = time.monotonic() - t_signal

        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not _pid_alive(child_pid), (
            f"fake upstream child pid {child_pid} was orphaned: still alive after the "
            "parent exited following a SIGTERM landing mid-connect"
        )
        assert parent_elapsed <= _MAX_SHUTDOWN_SECONDS
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stdin_eof_during_startup_does_not_orphan_the_spawned_child(tmp_path: Path) -> None:
    """The client-abandons-mid-connect scenario, but via stdin closing
    (no bytes ever written) rather than a signal -- proves the stdin-EOF
    watcher, not just the SIGTERM path, unblocks a stuck connect and kills
    the spawned child."""
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_hanging_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(pid_file, timeout=15.0)
        child_pid = int(pid_file.read_text().strip())
        assert _pid_alive(child_pid), "fake upstream child never actually started"

        time.sleep(0.5)
        t_close = time.monotonic()
        assert proc.stdin is not None
        proc.stdin.close()

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of stdin closing "
                "during the connect window (stdin-EOF watcher regression)"
            )
        parent_elapsed = time.monotonic() - t_close

        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not _pid_alive(child_pid), (
            f"fake upstream child pid {child_pid} was orphaned: still alive after the "
            "parent exited following stdin closing mid-connect"
        )
        assert parent_elapsed <= _MAX_SHUTDOWN_SECONDS
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stdin_eof_still_detected_after_the_client_already_wrote_bytes(tmp_path: Path) -> None:
    """The realistic case: a real MCP client writes its ``initialize``
    request immediately on spawn, then later abandons the connection by
    closing its end of stdin without ever reading a reply. Those bytes sit
    unread in the pipe the whole time -- proving detection still fires
    (via POLLHUP, not a data-vs-EOF guess from `select()`) and that nothing
    about consuming/peeking those bytes breaks the shutdown path."""
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_hanging_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(pid_file, timeout=15.0)
        child_pid = int(pid_file.read_text().strip())
        assert _pid_alive(child_pid), "fake upstream child never actually started"

        assert proc.stdin is not None
        proc.stdin.write('{"jsonrpc": "2.0", "method": "initialize"}\n')
        proc.stdin.flush()
        time.sleep(0.5)
        t_close = time.monotonic()
        proc.stdin.close()

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of stdin closing "
                "with an unread 'initialize' request still queued"
            )
        parent_elapsed = time.monotonic() - t_close

        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not _pid_alive(child_pid)
        assert parent_elapsed <= _MAX_SHUTDOWN_SECONDS
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_stdin_watcher_does_not_busy_spin_the_cpu_during_a_hanging_connect(tmp_path: Path) -> None:
    """The HIGH finding: a real MCP client writes its `initialize` request
    immediately on spawn and keeps stdin open while a slow/hanging upstream
    connect is in flight. Registering `POLLIN` (level-triggered on those
    queued-but-unread bytes) made every `poll()` call return instantly for
    the entire startup window, busy-spinning a full CPU core (measured ~1
    CPU-second per wall second). Registering only `POLLHUP` lets the kernel
    block the full interval instead (measured ~0.0 CPU-seconds over a 7s
    window with the fix; matches the fix with a POLLIN+sleep fallback,
    ~0.5 CPU-seconds over 8s, comfortably)."""
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_hanging_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(pid_file, timeout=15.0)
        assert proc.stdin is not None
        proc.stdin.write('{"jsonrpc": "2.0", "method": "initialize"}\n')
        proc.stdin.flush()

        # Let the watcher settle into steady state, then measure a CPU-time
        # delta (not an absolute value) across a window entirely inside the
        # 30s connect_timeout the hanging upstream never satisfies.
        time.sleep(1.0)
        cpu_at_1s = _cpu_seconds(proc.pid)
        time.sleep(2.0)
        cpu_at_3s = _cpu_seconds(proc.pid)

        delta = cpu_at_3s - cpu_at_1s
        assert delta < 0.3, (
            f"parent burned {delta:.2f} CPU-seconds over a 2s window with a hung connect and "
            "stdin's 'initialize' bytes still queued -- stdin-EOF watcher busy-spin regression"
        )
    finally:
        if proc.stdin is not None:
            proc.stdin.close()
        proc.kill()
        proc.wait(timeout=5)


def _write_real_fastmcp_upstream_script(tmp_path: Path) -> Path:
    """A fake upstream that speaks real MCP over stdio (via the installed
    fastmcp), so `start_all` actually completes and the site becomes
    healthy -- the "normal" shutdown path, as opposed to the startup-window
    one above."""
    script = tmp_path / "real_upstream.py"
    script.write_text(
        "import os, sys\n"
        "if '--version' not in sys.argv:\n"
        "    with open(sys.argv[1], 'w') as f:\n"
        "        f.write(str(os.getpid()))\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fake-upstream')\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    return script


def _write_two_process_upstream_scripts(tmp_path: Path) -> tuple[Path, Path]:
    """A ``wrapper.py`` that spawns ``leaf.py`` (a real fastmcp stdio server,
    same as ``_write_real_fastmcp_upstream_script``) as its OWN child,
    forwards SIGTERM to it, and blocks in ``wait()`` -- the shape of the real
    upstream (``uvx mcp-atlassian``: the ``uv`` wrapper plus its own Python
    leaf process) needed to reproduce the M4a round-4 BLOCKER: SIGSTOPping
    only the LEAF must not let a plain SIGTERM to the WRAPPER resolve the
    hang, since a stopped process can't act on a forwarded signal until
    resumed -- only a SIGKILL delivered to the whole process group can. A
    single-process fake upstream can't catch this: with only one process,
    there both the connect handle and the OS-signalable target are the same
    process, so closing it always has a real, directly-signalable pid.
    """
    leaf = tmp_path / "leaf.py"
    leaf.write_text(
        "import os, sys\n"
        "if '--version' not in sys.argv:\n"
        "    with open(sys.argv[1], 'w') as f:\n"
        "        f.write(str(os.getpid()))\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fake-leaf')\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import signal, subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, sys.argv[1], *sys.argv[2:]])\n"
        "def _forward(signum, frame):\n"
        "    try:\n"
        "        child.send_signal(signum)\n"
        "    except ProcessLookupError:\n"
        "        pass\n"
        "signal.signal(signal.SIGTERM, _forward)\n"
        "sys.exit(child.wait())\n"
    )
    return wrapper, leaf


def _complete_initialize_handshake(proc: subprocess.Popen[str], *, timeout: float = 15.0) -> None:
    """Completes a minimal real MCP ``initialize`` handshake over ``proc``'s
    stdio so the server has FULLY finished startup -- its startup-window
    stdin-EOF watcher has already been told to stop and
    ``mcp.run_stdio_async()`` is the sole reader of fd 0 -- before the test
    closes stdin. A site merely reporting ``healthy`` in the child log is not
    enough: `probe_upstream_version`/`discover_tools`/building the mirrored
    tool set can still be in flight for a bit after that, and closing stdin
    during that window exercises the (separately proven, M2r2) startup-EOF
    watcher instead of the normal post-startup shutdown path this test needs.
    """
    assert proc.stdin is not None and proc.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    proc.stdin.write(json.dumps(request) + "\n")
    proc.stdin.flush()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.5)
        if not ready:
            continue
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout before completing the initialize handshake")
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 1:
            notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
            proc.stdin.write(json.dumps(notification) + "\n")
            proc.stdin.flush()
            return
    raise TimeoutError(f"server never completed the initialize handshake within {timeout}s")


def _parent_pid(pid: int) -> int:
    """``pid``'s own parent pid right now, via ``ps`` (portable across the
    Linux CI runner and local macOS). Used to find the wrapper from the
    already-known leaf pid rather than assuming the server's only direct
    child -- ``probe_upstream_version``'s own one-off ``--version`` probe
    spawns a second, transient instance of the same wrapper script that can
    still be alive at the moment this test looks."""
    out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip())


def test_sigterm_after_healthy_shuts_down_quickly(tmp_path: Path) -> None:
    """The cancel-before-close reordering (needed to unblock a child stuck
    mid-handshake, see the test above) must not slow down or break the
    ordinary case: a site that's already healthy shuts down well under the
    watchdog bound, not by riding it out."""
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_real_fastmcp_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_file(pid_file, timeout=15.0)
        child_pid = int(pid_file.read_text().strip())

        # Give the site a moment to actually finish the MCP handshake and
        # become healthy (not just spawned) before signaling.
        time.sleep(0.5)

        t_signal = time.monotonic()
        proc.send_signal(signal.SIGTERM)

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of a normal SIGTERM")
        parent_elapsed = time.monotonic() - t_signal

        assert proc.returncode == 0
        # Well under the watchdog: this is the healthy-child fast path, not
        # the one that has to wait out a stuck handshake.
        assert parent_elapsed < SHUTDOWN_WATCHDOG_SECONDS

        deadline = time.monotonic() + 5.0
        while _pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(child_pid)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_sigstopped_child_does_not_delay_shutdown_past_the_watchdog(tmp_path: Path) -> None:
    """The LOW finding: `stack.aclose()`'s own exit (the exit stack's ordinary,
    non-forced `__aexit__` on each originally-connected client) must be bounded
    by the same watchdog budget as `manager.aclose()` -- an unkillable child
    (SIGSTOP'd, so not even SIGKILL can be delivered until it's resumed) must
    not make the parent ride out an indefinite wait on top of the bounded
    force-close."""
    pid_file = tmp_path / "child.pid"
    upstream_script = _write_real_fastmcp_upstream_script(tmp_path)
    config_path = _write_config(tmp_path, command=[sys.executable, str(upstream_script), str(pid_file)])

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid: int | None = None
    try:
        _wait_for_file(pid_file, timeout=15.0)
        child_pid = int(pid_file.read_text().strip())
        time.sleep(0.5)  # let the site actually finish the handshake, not just spawn

        os.kill(child_pid, signal.SIGSTOP)
        t_signal = time.monotonic()
        assert proc.stdin is not None
        proc.stdin.close()  # the real trigger this reproduces: the client goes away

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of stdin closing with a SIGSTOP'd child"
            )
        finally:
            # SIGCONT before anything else touches child_pid again -- a
            # stopped process can't even be waited on/reaped normally.
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGCONT)
        parent_elapsed = time.monotonic() - t_signal

        print(f"parent exited {parent_elapsed:.2f}s after stdin closed with a SIGSTOP'd child")
        assert parent_elapsed < _MAX_SHUTDOWN_SECONDS
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGCONT)
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, signal.SIGKILL)


def test_sigstopped_grandchild_in_a_two_process_upstream_does_not_delay_shutdown(tmp_path: Path) -> None:
    """The M4a round-4 BLOCKER: the real upstream (``uvx mcp-atlassian``) is
    a TWO-process tree -- a ``uv`` wrapper plus its own Python leaf child --
    and this round's earlier fix only proved the SINGLE-process case above.
    SIGSTOPping just the LEAF (never the wrapper, which stays alive,
    forwarding signals and blocked in its own ``wait()``, exactly like real
    ``uv``) must not let shutdown ride out an indefinite wait: a plain
    SIGTERM can't act on a stopped process, only a SIGKILL delivered to the
    whole process group can, and only the wrapper -- not the leaf -- is ever
    directly reachable through the connect handle this server holds."""
    pid_file = tmp_path / "child.pid"
    wrapper_script, leaf_script = _write_two_process_upstream_scripts(tmp_path)
    config_path = _write_config(
        tmp_path, command=[sys.executable, str(wrapper_script), str(leaf_script), str(pid_file)]
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    leaf_pid: int | None = None
    wrapper_pid: int | None = None
    try:
        _wait_for_file(pid_file, timeout=15.0)
        leaf_pid = int(pid_file.read_text().strip())
        assert _pid_alive(leaf_pid), "fake upstream leaf never actually started"
        # Completes a real `initialize` round trip -- startup (including the
        # version probe and tool discovery) is fully over and
        # `run_stdio_async()` is the sole reader of fd 0 by the time this
        # returns, so closing stdin next exercises the NORMAL post-startup
        # shutdown path, not the separately-proven startup-EOF watcher.
        _complete_initialize_handshake(proc, timeout=15.0)

        wrapper_pid = _parent_pid(leaf_pid)
        assert wrapper_pid != leaf_pid, "the wrapper and leaf must be different processes"
        assert _parent_pid(wrapper_pid) == proc.pid, (
            f"wrapper {wrapper_pid} is not a direct child of the server {proc.pid}"
        )

        os.kill(leaf_pid, signal.SIGSTOP)
        t_signal = time.monotonic()
        assert proc.stdin is not None
        proc.stdin.close()  # the real trigger this reproduces: the client goes away

        try:
            proc.wait(timeout=_MAX_SHUTDOWN_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            pytest.fail(
                f"parent did not exit within {_MAX_SHUTDOWN_SECONDS}s of stdin closing with a "
                "SIGSTOP'd grandchild in a two-process upstream tree"
            )
        finally:
            # SIGCONT before anything else touches leaf_pid again -- a
            # stopped process can't even be waited on/reaped normally.
            with contextlib.suppress(ProcessLookupError):
                os.kill(leaf_pid, signal.SIGCONT)
        parent_elapsed = time.monotonic() - t_signal

        print(f"parent exited {parent_elapsed:.2f}s after stdin closed with a SIGSTOP'd grandchild")
        assert parent_elapsed < _MAX_SHUTDOWN_SECONDS

        # The discriminating assertion: the parent exiting in time is not
        # enough on its own -- `os._exit` from a watchdog would satisfy it
        # even with both descendants orphaned. Both the wrapper AND the leaf
        # must actually be gone.
        deadline = time.monotonic() + 5.0
        while (_pid_alive(wrapper_pid) or _pid_alive(leaf_pid)) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _pid_alive(wrapper_pid), f"wrapper pid {wrapper_pid} was orphaned"
        assert not _pid_alive(leaf_pid), f"leaf pid {leaf_pid} was orphaned"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        for pid in (leaf_pid, wrapper_pid):
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGCONT)
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)


def test_schema_conflict_still_exits_2_not_an_exceptiongroup(tmp_path: Path) -> None:
    """`build_mirrored_tools` now runs inside the same task group as the
    shutdown-signal handler (needed to arm the receiver before `start_all`,
    see `serve`'s module docstring) -- anyio 4 wraps ANY exception escaping a
    task group, even one raised directly in the group's own body, in an
    ExceptionGroup. Proves the stash-and-reraise in `serve()` actually keeps
    `cli.main`'s `except McpAtlassianBetterError` -> exit-2 handling intact rather
    than letting a raw ExceptionGroup escape."""
    pid_file = tmp_path / "child.pid"
    script = tmp_path / "conflicting_upstream.py"
    script.write_text(
        "import os, sys\n"
        "with open(sys.argv[1], 'w') as f:\n"
        "    f.write(str(os.getpid()))\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('fake-upstream')\n"
        "@mcp.tool\n"
        "def jira_get_issue(site: str) -> dict:\n"
        "    return {'site': site}\n"
        "mcp.run(transport='stdio', show_banner=False)\n"
    )
    config_path = _write_config(tmp_path, command=[sys.executable, str(script), str(pid_file)])

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from mcp_atlassian_better.cli import main; sys.exit(main())",
            "--config",
            str(config_path),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == 2
    assert "error:" in completed.stderr
    assert "ExceptionGroup" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_force_exit_returns_0_when_aclose_completed() -> None:
    script = (
        "from mcp_atlassian_better.server import _force_exit, _ShutdownState\n"
        "_force_exit(_ShutdownState(aclose_completed=True))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], timeout=10)
    assert completed.returncode == 0


def test_force_exit_returns_1_when_aclose_did_not_complete() -> None:
    script = (
        "from mcp_atlassian_better.server import _force_exit, _ShutdownState\n"
        "_force_exit(_ShutdownState(aclose_completed=False))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], timeout=10)
    assert completed.returncode == 1
