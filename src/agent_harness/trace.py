from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any


class TraceWriter:
    """Append-only, hash-chained JSONL trace.

    Every event includes the hash of the previous event. This makes truncated or edited traces
    detectable while keeping the format easy to inspect and aggregate.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._previous_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        last = ""
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = line
        if not last:
            return "0" * 64
        return str(json.loads(last)["event_hash"])

    def append(self, event: str, **payload: Any) -> dict[str, Any]:
        record = {
            "schema_version": 1,
            "timestamp_ns": time.time_ns(),
            "event": event,
            "payload": payload,
            "previous_hash": self._previous_hash,
        }
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        record["event_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        self._previous_hash = record["event_hash"]
        return record


def verify_trace(path: Path) -> tuple[bool, int]:
    previous_hash = "0" * 64
    count = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            record = json.loads(line)
            event_hash = record.pop("event_hash")
            canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            expected = hashlib.sha256(canonical.encode()).hexdigest()
            if record["previous_hash"] != previous_hash or event_hash != expected:
                return False, count
            previous_hash = event_hash
            count += 1
    return True, count
