"""Tests for the process ownership layer in pwproc.

The one that matters here is the stdout/stderr drain: a pw-cli session
owning a DSP module (or a pw-cat keepalive) that logs more than the pipe
buffer holds must not block in write()."""

import sys
import time

from pwproc import OwnedPwProcess


def test_chatty_helper_does_not_block_on_a_full_pipe(tmp_path):
    """A helper that writes ~500 KiB to stdout (well past the 64 KiB pipe
    buffer) must not wedge: the drain threads keep the pipe empty, so the
    process gets through its output and on to the next thing it does."""
    marker = tmp_path / "reached"
    child = (
        "import pathlib, sys, time\n"
        "sys.stdout.write('x' * 500000)\n"
        "sys.stdout.flush()\n"
        f"pathlib.Path({str(marker)!r}).write_text('ok')\n"
        "time.sleep(30)\n"
    )
    owned = OwnedPwProcess("chatty", settle=0.5)
    try:
        assert owned.create([sys.executable, "-c", child]) is True
        deadline = time.time() + 5
        while time.time() < deadline and not marker.exists():
            time.sleep(0.05)
        assert marker.exists(), (
            "helper blocked writing to its stdout pipe (no drain)"
        )
    finally:
        owned.destroy()
