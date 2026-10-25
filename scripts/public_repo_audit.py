#!/usr/bin/env python3
"""Fail when the public tree contains credentials, personal paths, or runtime state."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_FILES = {".env", ".DS_Store"}
BLOCKED_SUFFIXES = {".sqlite", ".sqlite3", ".db"}
PATTERNS = {
    "macOS user path": re.compile(b"/" + rb"Users/[^/\s]+/"),
    "Linux home path": re.compile(b"/" + rb"home/[^/\s]+/"),
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    "API token": re.compile(rb"sk-[A-Za-z0-9_-]{20,}"),
}


def tracked_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / value.decode() for value in output.split(b"\0") if value]


def main() -> int:
    findings: list[str] = []
    for path in tracked_files():
        relative = path.relative_to(ROOT)
        if path.name in BLOCKED_FILES or path.suffix in BLOCKED_SUFFIXES:
            findings.append(f"blocked file type: {relative}")
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{label}: {relative}")

    if findings:
        print("Public repository audit failed:")
        for finding in findings:
            print(f"- {finding}")
        return 1
    print("Public repository audit passed: no credentials, personal home paths, or runtime databases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
