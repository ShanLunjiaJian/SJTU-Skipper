"""Serve completed byte ranges while the recording downloads."""

from __future__ import annotations

import http.server
import logging
import os
import re
import secrets
import threading
from pathlib import Path

BLOCK_SIZE = 2 * 1024 * 1024
IO_SIZE = 64 * 1024


class MediaCache:
    def __init__(self, downloader, url, path, stop_event, progress):
        self.downloader = downloader
        self.url = url
        self.path = Path(path)
        self.part = self.path.with_suffix(self.path.suffix + ".part")
        self.stop_event = stop_event
        self.progress = progress
        self.condition = threading.Condition()
        self.complete = set()
        self.active = set()
        self.demand = []
        self.total = 0
        self.sequential_end = 0
        self.prefetch = False
        self.ranged = False
        self.validator = None
        self.error = ""
        self.closed = False
        self.close_lock = threading.Lock()
        self.done = threading.Event()
        self.workers = []
        self.server = None
        self.server_thread = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.part, os.O_CREAT | os.O_TRUNC | os.O_RDWR, 0o600)

    def cancelled(self):
        return self.closed or (self.stop_event is not None and self.stop_event.is_set())

    def check(self):
        if self.cancelled():
            raise OSError("cancelled")
        if self.error:
            raise OSError(self.error)

    def start(self):
        try:
            self.check()
            with self.downloader._make_client() as client:
                with client.stream(
                    "GET", self.url, headers={"Range": "bytes=0-0"}
                ) as response:
                    response.raise_for_status()
                    self.validator = response.headers.get(
                        "etag"
                    ) or response.headers.get("last-modified")
                    if response.status_code == 206:
                        match = re.fullmatch(
                            r"bytes 0-0/(\d+)",
                            response.headers.get("content-range", ""),
                        )
                        if not match or sum(map(len, response.iter_bytes())) != 1:
                            raise OSError("Invalid range probe")
                        self.total = int(match[1])
                        self.ranged = True
                    else:
                        self.total = int(response.headers.get("content-length", 0))
            self.progress.total = self.total
            if self.ranged:
                self.downloader.acquire_connection(self, True)
                try:
                    self.fetch(0)
                finally:
                    self.downloader.release_connection()
            target = self.range_worker if self.ranged else self.stream_worker
            count = self.downloader._connections if self.ranged else 1
            self.workers = [
                threading.Thread(target=target, name="media-cache", daemon=True)
                for _ in range(count)
            ]
            for worker in self.workers:
                worker.start()
            if not self.total:
                self.done.wait()
                self.check()
                return str(self.path)
            self.serve()
            return self.input_url
        except Exception as exc:
            self.fail(exc)
            self.close()
            raise

    def fail(self, exc):
        with self.condition:
            if not self.error:
                self.error = str(exc)
            self.condition.notify_all()
            self.done.set()

    def fetch(self, index):
        start = index * BLOCK_SIZE
        end = min(self.total, start + BLOCK_SIZE) - 1
        temporary = self.part.with_name(self.part.name + f".chunk{index}")
        try:
            for attempt in range(4):
                try:
                    self.check()
                    headers = {"Range": f"bytes={start}-{end}"}
                    if self.validator:
                        headers["If-Range"] = self.validator
                    with self.downloader._make_client() as client:
                        with client.stream(
                            "GET", self.url, headers=headers
                        ) as response:
                            expected = f"bytes {start}-{end}/{self.total}"
                            if (
                                response.status_code != 206
                                or response.headers.get("content-range") != expected
                            ):
                                raise OSError("Source range or resource changed")
                            validator = response.headers.get(
                                "etag"
                            ) or response.headers.get("last-modified")
                            if self.validator and validator != self.validator:
                                raise OSError("Source resource changed")
                            size = 0
                            with temporary.open("wb") as output:
                                for data in response.iter_bytes(IO_SIZE):
                                    self.check()
                                    size += len(data)
                                    if size > end - start + 1:
                                        raise OSError("Oversized range response")
                                    output.write(data)
                            if size != end - start + 1:
                                raise OSError("Truncated range response")
                    with temporary.open("rb") as source:
                        offset = start
                        while data := source.read(IO_SIZE):
                            os.pwrite(self.fd, data, offset)
                            offset += len(data)
                    with self.condition:
                        self.complete.add(index)
                        self.active.discard(index)
                        self.condition.notify_all()
                    self.progress.add(size)
                    return
                except Exception:
                    if attempt == 3 or self.cancelled():
                        raise
        finally:
            temporary.unlink(missing_ok=True)

    def finish(self):
        with self.condition:
            if self.done.is_set():
                return
            os.replace(self.part, self.path)
            self.done.set()
            self.condition.notify_all()
        self.progress.finish()

    def range_worker(self):
        try:
            while True:
                with self.condition:
                    if self.done.is_set():
                        return
                    self.check()
                    count = (self.total + BLOCK_SIZE - 1) // BLOCK_SIZE
                    candidates = self.demand + list(range(count))
                    index = next(
                        (
                            i
                            for i in candidates
                            if i not in self.complete and i not in self.active
                        ),
                        None,
                    )
                    if index is None:
                        if len(self.complete) == count:
                            self.finish()
                            return
                        self.condition.wait(0.1)
                        continue
                    demanded = index in self.demand
                    self.active.add(index)
                    self.demand = [i for i in self.demand if i != index]
                self.downloader.acquire_connection(self, demanded)
                try:
                    with self.condition:
                        urgent = next(
                            (
                                i
                                for i in self.demand
                                if i not in self.complete and i not in self.active
                            ),
                            None,
                        )
                        if urgent is not None and not demanded:
                            self.active.discard(index)
                            index = urgent
                            self.active.add(index)
                            self.demand.remove(index)
                    self.fetch(index)
                finally:
                    self.downloader.release_connection()
        except Exception as exc:
            if not self.done.is_set():
                self.fail(exc)

    def stream_worker(self):
        acquired = False
        try:
            self.downloader.acquire_connection(self, False)
            acquired = True
            with self.downloader._make_client() as client:
                with client.stream("GET", self.url) as response:
                    response.raise_for_status()
                    validator = response.headers.get("etag") or response.headers.get(
                        "last-modified"
                    )
                    if self.validator and validator != self.validator:
                        raise OSError("Source resource changed")
                    for data in response.iter_bytes(IO_SIZE):
                        self.check()
                        os.pwrite(self.fd, data, self.sequential_end)
                        with self.condition:
                            self.sequential_end += len(data)
                            self.condition.notify_all()
                        self.progress.add(len(data))
            if self.total and self.sequential_end != self.total:
                raise OSError("Truncated media response")
            self.total = self.sequential_end
            self.finish()
        except Exception as exc:
            self.fail(exc)
        finally:
            if acquired:
                self.downloader.release_connection()

    def read(self, offset, size):
        with self.condition:
            while True:
                self.check()
                if self.ranged:
                    index = offset // BLOCK_SIZE
                    if index in self.complete:
                        size = min(size, BLOCK_SIZE - offset % BLOCK_SIZE)
                        break
                    if index not in self.demand:
                        self.demand.insert(0, index)
                        self.condition.notify_all()
                elif self.sequential_end > offset:
                    size = min(size, self.sequential_end - offset)
                    break
                self.condition.wait(0.1)
            data = os.pread(self.fd, size, offset)
        if len(data) != size:
            raise OSError("Incomplete cached range")
        return data

    def serve(self):
        cache = self
        token = "/" + secrets.token_urlsafe(24)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                pass

            def do_HEAD(self):
                self.respond(False)

            def do_GET(self):
                self.respond(True)

            def respond(self, body):
                if self.path != token:
                    self.send_error(404)
                    return
                start, end = 0, cache.total - 1
                value = self.headers.get("Range")
                if value:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
                    if not match or not any(match.groups()):
                        self.send_error(416)
                        return
                    if match[1]:
                        start = int(match[1])
                        end = min(end, int(match[2])) if match[2] else end
                    else:
                        start = max(0, cache.total - int(match[2]))
                    if (
                        start > end
                        or start >= cache.total
                        or (not match[1] and int(match[2]) == 0)
                    ):
                        self.send_error(416)
                        return
                try:
                    first = (
                        cache.read(start, min(IO_SIZE, end - start + 1))
                        if body
                        else b""
                    )
                except OSError:
                    self.send_error(503)
                    return
                self.send_response(206 if value else 200)
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                if value:
                    self.send_header(
                        "Content-Range", f"bytes {start}-{end}/{cache.total}"
                    )
                self.end_headers()
                if not body:
                    return
                try:
                    while start <= end:
                        data = first or cache.read(start, min(IO_SIZE, end - start + 1))
                        first = b""
                        self.wfile.write(data)
                        start += len(data)
                except (OSError, ConnectionError) as exc:
                    logging.getLogger(__name__).debug(
                        "Cache response interrupted: %s", exc
                    )
                    self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.input_url = f"http://127.0.0.1:{self.server.server_port}{token}"
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, name="media-http", daemon=True
        )
        self.server_thread.start()

    def close(self):
        with self.close_lock:
            with self.condition:
                self.closed = True
                self.condition.notify_all()
            for worker in self.workers:
                if worker is not threading.current_thread():
                    worker.join()
            if self.server:
                self.server.shutdown()
                self.server.server_close()
                if self.server_thread is not None:
                    self.server_thread.join()
                self.server = None
            with self.condition:
                if self.fd >= 0:
                    os.close(self.fd)
                    self.fd = -1
            if not self.path.exists():
                self.part.unlink(missing_ok=True)
            self.done.set()
