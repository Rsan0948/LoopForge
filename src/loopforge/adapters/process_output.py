"""Bounded pipe capture shared by container execution adapters."""

import os
import selectors
import subprocess
import time
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    stdout: str
    stderr: str
    truncated: bool
    # Infrastructure classification uses captured bytes before display markers.
    stderr_prefix: str


def capture_output(process: subprocess.Popen[bytes], timeout: float, limit: int) -> CapturedOutput:
    assert process.stdout is not None
    assert process.stderr is not None
    streams = (process.stdout, process.stderr)
    buffers = (bytearray(), bytearray())
    truncated = [False, False]
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate(streams):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                ready = selector.select(min(remaining, 0.05))
                for key, _ in ready:
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    index = cast("int", key.data)
                    available = limit - len(buffers[index])
                    buffers[index].extend(data[:available])
                    truncated[index] |= len(data) > available
                if process.poll() is not None:
                    # A writer can exit just after filling a pipe. Drain the
                    # remaining prefix without waiting for inherited handles.
                    # Stop after cap+1 bytes even if a descendant keeps writing.
                    for pending in list(selector.get_map().values()):
                        index = cast("int", pending.data)
                        if not truncated[index]:
                            truncated[index] = _drain_exited_pipe(pending.fd, buffers[index], limit)
                    break
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
    finally:
        close_pipes(process)
    stdout, stdout_cut = _render_output(buffers[0], truncated[0], limit)
    stderr, stderr_cut = _render_output(buffers[1], truncated[1], limit)
    return CapturedOutput(
        stdout,
        stderr,
        stdout_cut or stderr_cut,
        bytes(buffers[1][:300]).decode("utf-8", errors="replace"),
    )


def _drain_exited_pipe(fd: int, buffer: bytearray, limit: int) -> bool:
    """Drain an exited command's available prefix; never wait on descendants."""
    while True:
        available = limit - len(buffer)
        try:
            tail = os.read(fd, min(65536, available + 1))
        except BlockingIOError:
            return False
        if not tail:
            return False
        buffer.extend(tail[:available])
        if len(tail) > available:
            return True


def _render_output(data: bytearray, overflow: bool, limit: int) -> tuple[str, bool]:
    """Bound UTF-8 text bytes after replacement, including the truncation marker."""
    decoded = data.decode("utf-8", errors="replace")
    encoded = decoded.encode("utf-8")
    overflow = overflow or len(encoded) > limit
    if not overflow:
        return decoded, False
    marker = b"\n[output truncated]"[:limit]
    prefix = encoded[: limit - len(marker)].decode("utf-8", errors="ignore")
    return prefix + marker.decode("utf-8"), True


def close_pipes(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()
