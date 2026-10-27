"""Platform portability of the agents-registry lock in ``agent_detect``.

``agent_detect`` imported ``fcntl`` at module scope, which made the module
(and with it the setup flow and the whole test-suite via conftest) unimportable
on Windows. These tests pin the replacement contract:

1. The module imports and works with no ``fcntl`` at all.
2. On Windows the ``msvcrt`` branch really takes and releases a lock (lock on
   one byte at offset 0, ``LK_NBLCK`` + ``LK_UNLCK``), including the shared
   request that degrades to exclusive there.
3. With neither backend the module still registers agents and warns exactly
   once instead of failing.

Each scenario needs a module imported under different platform conditions, so
the helper below imports a FRESH module instance and restores ``sys.modules``
afterwards — otherwise the rest of the suite would inherit a lockless module.
"""

import contextlib
import errno
import importlib
import logging
import os
import sys

import pytest

_MISSING = object()


class _BlockImport:
    """Meta-path finder that refuses to import the named modules."""

    def __init__(self, *names):
        self.names = frozenset(names)

    def find_spec(self, fullname, path=None, target=None):
        if fullname in self.names:
            raise ImportError(f"{fullname} blocked for this test")
        return None


class _FakeMsvcrt:
    """Minimal stand-in for the Windows ``msvcrt`` module.

    Records every ``locking`` call as ``(mode, nbytes, position)``. With
    ``failures_before_success`` the first N non-blocking lock attempts raise
    ``OSError(EACCES)``, the way a lock held by another process does.
    """

    LK_UNLCK = 0
    LK_LOCK = 1
    LK_NBLCK = 2
    LK_RLCK = 3
    LK_NBRLCK = 4

    def __init__(self, failures_before_success=0):
        self.calls = []
        self.failures_before_success = failures_before_success

    def locking(self, fd, mode, nbytes):
        # Position matters: msvcrt locks relative to the current offset, so
        # the implementation must seek to 0 before every call.
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_NBLCK and self.failures_before_success > 0:
            self.failures_before_success -= 1
            raise OSError(errno.EACCES, "locked by another process")


@contextlib.contextmanager
def _fresh_agent_detect(*, block_fcntl=True, block_msvcrt=False,
                        fake_msvcrt=None):
    """Import ``agent_detect`` fresh under the requested platform conditions.

    ``sys.modules`` is restored afterwards (including the package attribute),
    so the already-imported module object other tests hold keeps working.
    """
    import nexus_memory

    original = sys.modules.pop("nexus_memory.agent_detect")
    saved_fcntl = sys.modules.pop("fcntl", _MISSING)
    saved_msvcrt = sys.modules.pop("msvcrt", _MISSING)

    blocked = []
    if block_fcntl:
        sys.modules["fcntl"] = None  # a None entry makes ``import`` fail
        blocked.append("fcntl")
    if fake_msvcrt is not None:
        sys.modules["msvcrt"] = fake_msvcrt
    elif block_msvcrt:
        sys.modules["msvcrt"] = None
        blocked.append("msvcrt")

    finder = _BlockImport(*blocked)
    sys.meta_path.insert(0, finder)
    try:
        yield importlib.import_module("nexus_memory.agent_detect")
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
        for name, saved in (("fcntl", saved_fcntl), ("msvcrt", saved_msvcrt)):
            if saved is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
        sys.modules["nexus_memory.agent_detect"] = original
        nexus_memory.agent_detect = original


@pytest.fixture
def registry_path(tmp_path):
    return tmp_path / "agents.json"


def test_module_imports_without_fcntl(monkeypatch, registry_path):
    """No fcntl -> import succeeds and the registry API still works."""
    with _fresh_agent_detect(block_fcntl=True) as ad:
        assert ad._HAVE_FCNTL is False
        assert ad.fcntl is None

        monkeypatch.setattr(ad, "_get_agents_registry_path",
                            lambda: registry_path)

        result = ad.register_agent(
            "agent-win-1", "Test Agent", "*", "trusted", "mcp_only")
        assert result.get("error") is None
        assert result["id"] == "agent-win-1"

        loaded = ad.load_agents_registry()
        assert [a["id"] for a in loaded["agents"]] == ["agent-win-1"]

        ad.save_agents_registry({"agents": [{"id": "saved-directly"}]})
        assert [a["id"] for a in ad.load_agents_registry()["agents"]] == [
            "saved-directly"]


def test_windows_branch_locks_one_byte_exclusively(monkeypatch, registry_path):
    """msvcrt branch: LK_NBLCK on byte 0 at offset 0, released with LK_UNLCK."""
    fake = _FakeMsvcrt()
    with _fresh_agent_detect(block_fcntl=True,
                             fake_msvcrt=fake) as ad:
        assert ad._HAVE_FCNTL is False
        assert ad._HAVE_MSVCRT is True
        monkeypatch.setattr(ad, "_get_agents_registry_path",
                            lambda: registry_path)

        lock_path = ad._registry_lock_path()
        with open(lock_path, "a+") as lock_file:
            ad._lock_file(lock_file.fileno(), exclusive=True)
            ad._unlock_file(lock_file.fileno())

        assert fake.calls == [
            (fake.LK_NBLCK, 1, 0),
            (fake.LK_UNLCK, 1, 0),
        ]

        # A shared request has no msvcrt equivalent: it locks exclusively.
        fake.calls.clear()
        with open(lock_path, "a+") as lock_file:
            ad._lock_file(lock_file.fileno(), exclusive=False)
            ad._unlock_file(lock_file.fileno())

        assert fake.calls == [
            (fake.LK_NBLCK, 1, 0),
            (fake.LK_UNLCK, 1, 0),
        ]


def test_windows_branch_used_by_register_agent(monkeypatch, registry_path):
    """The registry read-modify-write runs inside a balanced msvcrt lock."""
    fake = _FakeMsvcrt()
    with _fresh_agent_detect(block_fcntl=True,
                             fake_msvcrt=fake) as ad:
        monkeypatch.setattr(ad, "_get_agents_registry_path",
                            lambda: registry_path)

        ad.register_agent("agent-win-2", "Test Agent", "*", "trusted",
                          "mcp_only")

    locks = [c for c in fake.calls if c[0] == fake.LK_NBLCK]
    unlocks = [c for c in fake.calls if c[0] == fake.LK_UNLCK]
    assert len(locks) == 1
    assert len(unlocks) == 1
    assert all(c[1:] == (1, 0) for c in locks + unlocks)
    assert registry_path.exists()


def test_windows_lock_retries_until_available(monkeypatch, registry_path):
    """A lock held elsewhere is retried instead of failing on first EACCES."""
    fake = _FakeMsvcrt(failures_before_success=2)
    with _fresh_agent_detect(block_fcntl=True,
                             fake_msvcrt=fake) as ad:
        monkeypatch.setattr(ad, "_LOCK_ACQUIRE_TIMEOUT_SEC", 5.0)
        monkeypatch.setattr(ad, "_LOCK_RETRY_INTERVAL_SEC", 0)

        lock_path = ad._registry_lock_path()
        with open(lock_path, "a+") as lock_file:
            ad._lock_file(lock_file.fileno(), exclusive=True)
            ad._unlock_file(lock_file.fileno())

        assert [c[0] for c in fake.calls] == [
            fake.LK_NBLCK, fake.LK_NBLCK, fake.LK_NBLCK, fake.LK_UNLCK]


def test_windows_lock_times_out_with_clear_error(monkeypatch):
    """A never-released lock raises a readable TimeoutError, not a hang."""
    fake = _FakeMsvcrt(failures_before_success=10 ** 6)
    with _fresh_agent_detect(block_fcntl=True,
                             fake_msvcrt=fake) as ad:
        monkeypatch.setattr(ad, "_LOCK_ACQUIRE_TIMEOUT_SEC", 0)
        monkeypatch.setattr(ad, "_LOCK_RETRY_INTERVAL_SEC", 0)

        lock_path = ad._registry_lock_path()
        with open(lock_path, "a+") as lock_file:
            with pytest.raises(TimeoutError) as excinfo:
                ad._lock_file(lock_file.fileno(), exclusive=True)

        assert "registry lock" in str(excinfo.value)


def test_no_lock_backend_warns_once_and_still_writes(
        monkeypatch, registry_path, caplog):
    """Neither backend: one warning, and the registry keeps working."""
    with _fresh_agent_detect(block_fcntl=True, block_msvcrt=True) as ad:
        assert ad._HAVE_FCNTL is False
        assert ad._HAVE_MSVCRT is False
        monkeypatch.setattr(ad, "_get_agents_registry_path",
                            lambda: registry_path)

        with caplog.at_level(logging.WARNING):
            ad.register_agent("agent-freebsd-1", "Test Agent", "*",
                              "trusted", "mcp_only")
            ad.register_agent("agent-freebsd-2", "Test Agent", "*",
                              "trusted", "mcp_only")

        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING
                    and "registry lock unavailable" in r.getMessage()]
        assert len(warnings) == 1

        ids = [a["id"] for a in ad.load_agents_registry()["agents"]]
        assert ids == ["agent-freebsd-1", "agent-freebsd-2"]
