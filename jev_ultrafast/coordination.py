"""Cross-process coordination: agent identity, origin locks, safe JSONL appends.

Multiple agents can drive the same Chrome at once (one browser-harness daemon
per BU_NAME, one tab per agent). What they must not do is fight over one site
or interleave result lines, so runs coordinate through OS-level file locks that
release automatically when a process dies — a crashed run cannot strand an
origin or corrupt a shared results file.
"""

import json
import os
import re
import time
import zlib
from pathlib import Path

IS_WINDOWS = os.name == "nt"
if IS_WINDOWS:
    import msvcrt
else:
    import fcntl


def agent_name():
    """This process's agent identity. JEV_AGENT_NAME wins so a coordinator can
    name its workers even when the operator's environment already carries BU_NAME."""
    return os.environ.get("JEV_AGENT_NAME") or os.environ.get("BU_NAME") or "default"


def locks_dir():
    return Path(os.environ.get("JEV_LOCKS_DIR") or Path.home() / ".jev-ultrafast" / "locks")


def _lock_path(host):
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", host)[:120] or "unknown"
    return locks_dir() / f"{slug}.lock"


def _try_lock(fd):
    """Non-blocking exclusive lock on byte 0; True when acquired."""
    try:
        if IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd):
    try:
        if IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class OriginLock:
    """One agent working on one site at a time.

    The lock lives on a per-host file under the locks dir. Holding it means
    this process may act and heal on the origin; other agents either wait or
    start elsewhere. Byte-range/flock locks are released by the OS when the
    holder exits by any means, so there is no stale-lock cleanup path. On
    Windows the locked region is unreadable by every handle, so holder identity
    comes from an in-process registry — file contents are debug notes only."""

    _held = {}  # host -> OriginLock this process holds (agents are one per process)

    def __init__(self, host):
        self.host = host
        self.path = _lock_path(host)
        self.fd = None

    def acquire(self, wait_s=0):
        """Take the lock, polling up to wait_s seconds. False when still held."""
        if self.fd is not None:
            return True
        locks_dir().mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, wait_s)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        while True:
            if _try_lock(fd):
                self.fd = fd
                OriginLock._held[self.host] = self
                try:  # Who holds the lock, for a human inspecting the locks dir.
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()} {agent_name()}\n".encode())
                except OSError:
                    pass
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)

    def release(self):
        if self.fd is None:
            return
        _unlock(self.fd)
        os.close(self.fd)
        self.fd = None
        if OriginLock._held.get(self.host) is self:
            del OriginLock._held[self.host]

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"origin {self.host} is locked by another agent")
        return self

    def __exit__(self, *_args):
        self.release()

    @classmethod
    def contested(cls, host):
        """True when a live process other than this one holds the origin.

        Our own locks are known in-process. Anything else is decided by a
        throwaway try-lock: a dead holder's lock is already gone, so the probe
        succeeds and healing may clear the abandoned session."""
        if host in cls._held:
            return False
        locks_dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(_lock_path(host), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if _try_lock(fd):
                _unlock(fd)
                return False
            return True
        finally:
            os.close(fd)


def append_record(path, record, wait_s=10):
    """One JSON line appended under an exclusive lock, so concurrent agents can
    share one results file without interleaving partial lines. Contended writers
    queue briefly rather than failing a finished run's evidence."""
    line = json.dumps(record) + "\n"
    deadline = time.monotonic() + wait_s
    with open(path, "a", encoding="utf-8") as f:
        fd = f.fileno()
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"results file stayed locked for {wait_s}s: {path}")
            time.sleep(0.02)
        try:
            f.write(line)
            f.flush()
        finally:
            _unlock(fd)


def worker_port(name):
    """A stable inspector port per agent name so several demo UIs can coexist.
    Operators keep TYPESAFE_DEMO_PORT for an explicit override."""
    return 8766 + zlib.crc32(name.encode()) % 100
