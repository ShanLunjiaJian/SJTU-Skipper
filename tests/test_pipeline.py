from __future__ import annotations

import threading
import time

from core.streams import BacklogGate


class TestBacklogGate:
    def test_disabled_gate_returns_immediately(self) -> None:
        gate = BacklogGate("audio", limit=0.0)
        assert gate.wait(10_000.0) is True

    def test_blocks_until_released(self) -> None:
        gate = BacklogGate("audio", limit=5.0)
        gate.configure(5.0)
        assert gate.wait(5.0) is True
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(gate.wait(10.0)))
        worker.start()
        time.sleep(0.2)
        assert result == []
        gate.release(10.0)
        worker.join(timeout=2.0)
        assert result == [True]

    def test_close_unblocks_waiter(self) -> None:
        gate = BacklogGate("video", limit=1.0)
        gate.configure(1.0)
        result: list[bool] = []
        worker = threading.Thread(target=lambda: result.append(gate.wait(60.0)))
        worker.start()
        time.sleep(0.2)
        gate.close()
        worker.join(timeout=2.0)
        assert result == [False]

    def test_configure_resets_released(self) -> None:
        gate = BacklogGate("audio", limit=5.0)
        gate.configure(5.0)
        gate.release(100.0)
        gate.configure(5.0)
        assert gate.released == 0.0
