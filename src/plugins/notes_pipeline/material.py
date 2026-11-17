from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from core.streams import StreamKind


def read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    return records


def shift_records(records: list[dict], shift: float) -> list[dict]:
    result = []
    for record in records:
        moved = dict(record)
        for key in ("start", "end"):
            if moved.get(key) is not None:
                moved[key] = float(moved[key]) + shift
        result.append(moved)
    return result


def fill_sessions(data_dir: Path, session_id: str) -> list[tuple[str, float]]:
    result = []
    primary = data_dir / "sessions" / session_id / "meta.json"
    primary_meta = (
        json.loads(primary.read_text(encoding="utf-8")) if primary.exists() else {}
    )
    started = float(primary_meta.get("started_at", 0) or 0)
    for path in sorted((data_dir / "sessions").glob("*/meta.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = raw.get("meta", raw)
        if meta.get("fill_for") == session_id:
            begin = float(meta.get("media_begin", 0) or 0)
            result.append(
                (path.parent.name, begin - started if begin and started else 0.0)
            )
    return result


def collect_material(ctx, session_id: str) -> list[dict]:
    result = []
    for sid, shift in [(session_id, 0.0), *fill_sessions(ctx.data_dir, session_id)]:
        session = ctx.streams.get_session(sid)
        for stream, filename in (
            (StreamKind.TRANSCRIPT, "transcript.jsonl"),
            (StreamKind.VISUAL, "visual.jsonl"),
        ):
            records = read_log(ctx.data_dir / "sessions" / sid / filename)
            if session is not None:

                def identity(record):
                    return (
                        round(float(record["start"]), 3)
                        if record.get("start") is not None
                        else None,
                        round(float(record["end"]), 3)
                        if record.get("end") is not None
                        else None,
                        record.get("text"),
                    )

                known = Counter(identity(record) for record in records)
                for item in getattr(session, stream).items_since(0):
                    record = {"text": item.text, "start": item.start, "end": item.end}
                    key = identity(record)
                    if known[key]:
                        known[key] -= 1
                    else:
                        records.append(record)
            for record in shift_records(records, shift):
                if stream == StreamKind.VISUAL and record.get("error"):
                    error = record["error"]
                    record["text"] = (
                        f"该时间段未取得板书：{error['message']} "
                        f"({error['code']}, attempts={error['attempts']})"
                    )
                if str(record.get("text", "")).strip():
                    result.append({**record, "stream": stream, "origin": sid})
    return sorted(
        result,
        key=lambda record: (
            float(record.get("start") or 0),
            record["stream"],
            record.get("index", 0),
        ),
    )


def render(records: list[dict]) -> str:
    entries = []
    for record in records:
        start = record.get("start")
        stamp = "manual" if start is None else f"{float(start):.3f}s"
        label = (
            ("板书识别缺口" if record.get("error") else "板书增量")
            if record["stream"] == StreamKind.VISUAL
            else "讲授"
        )
        if record.get("error") and record.get("end") is not None:
            stamp += f"-{float(record['end']):.3f}s"
        entries.append(f"[{stamp}] {label}\n{record['text']}")
    return "\n\n".join(entries) + ("\n" if entries else "")


def digest(records: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(records, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def chunk_material(records: list[dict], start: float, end: float, overlap: float):
    core = [
        record
        for record in records
        if record.get("start") is not None and start <= float(record["start"]) < end
    ]
    context = [
        record
        for record in records
        if record.get("start") is not None
        and float(record["start"]) < start
        and float(record.get("end") or record["start"]) >= max(0, start - overlap)
    ]
    previous_visual = [
        record
        for record in records
        if record["stream"] == StreamKind.VISUAL
        and record.get("start") is not None
        and float(record["start"]) < start
    ][-3:]
    for record in previous_visual:
        if record not in context:
            context.append(record)
    return core, context
