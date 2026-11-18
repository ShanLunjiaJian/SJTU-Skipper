"""Multiplex requests over the private Bun process pipes."""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from core.agent_types import IpcMethod, RuntimeFrameType


class RuntimeClient:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self._process = None
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, queue.Queue] = {}
        self._closed = False

    def _start(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Agent runtime is closed")
            if self._process is not None and self._process.poll() is None:
                return
            self.data_dir.mkdir(parents=True, exist_ok=True)
            entry = Path(__file__).with_name("main.ts")
            self._process = subprocess.Popen(
                ["bun", str(entry), str(self.data_dir)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
            process = self._process
            threading.Thread(target=self._read, args=(process,), daemon=True).start()
            threading.Thread(target=self._logs, args=(process,), daemon=True).start()

    def _logs(self, process):
        for line in process.stderr:
            logging.getLogger("lecture_agent.runtime.bun").info("%s", line.rstrip())

    def _read(self, process):
        try:
            for line in process.stdout:
                message = json.loads(line)
                message["type"] = RuntimeFrameType(message["type"])
                with self._lock:
                    target = self._pending.get(str(message.get("id", "")))
                if target is not None:
                    target.put(message)
        except Exception as exc:
            reason = str(exc)
        else:
            reason = f"Agent runtime exited ({process.wait()})"
        with self._lock:
            if self._process is process:
                for target in self._pending.values():
                    target.put({"type": RuntimeFrameType.ERROR, "error": reason})

    def stream(self, method: IpcMethod, *, on_sent=None, **params):
        if not isinstance(method, IpcMethod):
            raise TypeError("Runtime method must be an IpcMethod")
        with self._lock:
            if method in (IpcMethod.CANCEL, IpcMethod.CLOSE_SESSION, IpcMethod.MESSAGE):
                if (
                    self._closed
                    or self._process is None
                    or self._process.poll() is not None
                ):
                    if method == IpcMethod.MESSAGE:
                        raise RuntimeError("Agent runtime is closed")
                    return
            else:
                self._start()
        request_id = uuid.uuid4().hex
        target = queue.Queue()
        with self._lock:
            self._pending[request_id] = target
        try:
            with self._write_lock:
                process = self._process
                assert process is not None and process.stdin is not None
                process.stdin.write(
                    json.dumps(
                        {"id": request_id, "method": method, "params": params},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                process.stdin.flush()
            if on_sent is not None:
                on_sent()
            while True:
                message = target.get()
                if message["type"] == RuntimeFrameType.ERROR:
                    raise RuntimeError(message["error"])
                yield message
                if message["type"] == RuntimeFrameType.RESULT:
                    break
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def call(self, method: IpcMethod, **params) -> dict[str, Any]:
        for message in self.stream(method, **params):
            if message["type"] == RuntimeFrameType.RESULT:
                return message["result"]
        if method in (IpcMethod.CANCEL, IpcMethod.CLOSE_SESSION):
            return {}
        raise RuntimeError("Agent runtime returned no result")

    def cancel(self, session_id: str):
        return self.call(IpcMethod.CANCEL, session_id=session_id)

    def close(self):
        with self._lock:
            process = self._process
            self._closed = True
        if process is None:
            return
        if process.poll() is None:
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
