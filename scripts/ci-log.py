#!/usr/bin/env python3
"""Stream command output and retain at most 256 KiB of its tail, without failing the command."""

from __future__ import annotations

import sys
from pathlib import Path

LIMIT = 256 * 1024


def main() -> None:
    destination = Path(sys.argv[1])
    tail = bytearray()
    writable = True
    while chunk := sys.stdin.buffer.read1(16 * 1024):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        if writable:
            tail.extend(chunk)
            del tail[:-LIMIT]
            try:
                destination.write_bytes(tail)
            except OSError as exc:
                print(f"Command log unavailable: {exc}", file=sys.stderr)
                writable = False


if __name__ == "__main__":
    main()
