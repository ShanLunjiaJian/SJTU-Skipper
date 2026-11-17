import os
import threading
from concurrent.futures import ThreadPoolExecutor

from plugins.input.media_cache import MediaCache


def test_close_waits_for_read(tmp_path, monkeypatch):
    cache = MediaCache(None, "unused", tmp_path / "video", None, None)
    os.pwrite(cache.fd, b"video", 0)
    cache.sequential_end = 5
    entered = threading.Event()
    release = threading.Event()
    closing = threading.Event()
    pread = os.pread

    def read(fd, size, offset):
        entered.set()
        assert release.wait(3)
        return pread(fd, size, offset)

    def close():
        closing.set()
        cache.close()

    monkeypatch.setattr(os, "pread", read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reading = pool.submit(cache.read, 0, 5)
        try:
            assert entered.wait(3)
            closed = pool.submit(close)
            assert closing.wait(3)
            assert not closed.done()
        finally:
            release.set()
        assert reading.result(timeout=3) == b"video"
        closed.result(timeout=3)
    assert cache.fd == -1
