from __future__ import annotations

import threading
import time

from core.streams import MediaBufferLimiter


class TestMediaBufferLimiter:
    def test_disabled_limiter_returns_immediately(self) -> None:
        limiter = MediaBufferLimiter("audio", limit=0.0)
        assert limiter.wait(10_000.0) is True

    def test_blocks_until_released(self) -> None:
        limiter = MediaBufferLimiter("audio", limit=5.0)
        limiter.configure(5.0)
        assert limiter.wait(5.0) is True
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(limiter.wait(10.0)))
        worker.start()
        time.sleep(0.2)
        assert result == []
        limiter.release(10.0)
        worker.join(timeout=2.0)
        assert result == [True]

    def test_close_unblocks_waiter(self) -> None:
        limiter = MediaBufferLimiter("video", limit=1.0)
        limiter.configure(1.0)
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(limiter.wait(60.0)))
        worker.start()
        time.sleep(0.2)
        limiter.close()
        worker.join(timeout=2.0)
        assert result == [False]

    def test_configure_resets_released(self) -> None:
        limiter = MediaBufferLimiter("audio", limit=5.0)
        limiter.configure(5.0)
        limiter.release(100.0)
        limiter.configure(5.0)
        assert limiter.released == 0.0
