"""Reuse local asset digests until the filesystem reports a file change.

Content digests identify rendered assets and analysis results. Computing them
again at every cache lookup defeats that cache, so readers share this small
process-local memo. Nothing persists across application restarts.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable


MAX_FINGERPRINTS = 256
_Signature = tuple[int, int, int, int, int]
_Key = tuple[str, _Signature]
_digests: OrderedDict[_Key, str] = OrderedDict()
_inflight: dict[_Key, threading.Event] = {}
_lock = threading.RLock()


def _signature(stat: os.stat_result) -> _Signature:
    # ctime catches in-place edits even when an editor restores mtime; inode
    # and device catch atomic replacement and changed symlink destinations.
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def cached_file_digest(
    path: str | os.PathLike[str],
    *,
    max_bytes: int,
    expected_signature: tuple[int, int] | None,
    read_digest: Callable[[str], str],
) -> str:
    """Read a digest once per file version, sharing concurrent requests.

    The caller supplies its existing streaming reader and domain errors. The
    reader receives the resolved path used for the cache key, so a retargeted
    symlink cannot put another file's digest under that key. A cache hit uses
    only stat metadata, while changed files pass through the reader.
"""

    resolved = os.path.realpath(os.path.expanduser(os.fspath(path)))
    while True:
        stat = os.stat(resolved)
        if stat.st_size > max_bytes:
            raise ValueError(f"file exceeds the {max_bytes} byte cap")
        if expected_signature is not None and (stat.st_size, stat.st_mtime_ns) != expected_signature:
            raise ValueError("audio file changed before hashing")
        signature = _signature(stat)
        key = (resolved, signature)
        with _lock:
            cached = _digests.get(key)
            if cached is not None:
                _digests.move_to_end(key)
                return cached
            event = _inflight.get(key)
            if event is None:
                event = threading.Event()
                _inflight[key] = event
                break
        event.wait()
    try:
        digest = read_digest(resolved)
        if _signature(os.stat(resolved)) != signature:
            raise ValueError("audio file changed while it was being hashed")
        with _lock:
            _digests[key] = digest
            _digests.move_to_end(key)
            while len(_digests) > MAX_FINGERPRINTS:
                _digests.popitem(last=False)
        return digest
    finally:
        with _lock:
            _inflight.pop(key, None)
            event.set()
