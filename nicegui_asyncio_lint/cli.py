"""CLI entry point: `nicegui-asyncio-lint <file-or-dir> [...]`.

Exit code is 1 if any finding was reported (CI-friendly), 0 otherwise.
Usable directly, or as a pre-commit local hook (see README.md).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checker import check_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nicegui-asyncio-lint",
        description="Flag blocking sleep / blocking sync I/O / unbounded create_task / "
        "missing-await footguns in asyncio (incl. NiceGUI) code.",
    )
    parser.add_argument("paths", nargs="+", type=Path, help="files or directories to scan")
    parser.add_argument(
        "--select",
        default=None,
        help="comma-separated rule codes to restrict to, e.g. NGA001,NGA003",
    )
    args = parser.parse_args(argv)

    select = set(args.select.split(",")) if args.select else None

    findings = list(check_paths(args.paths))
    if select:
        findings = [f for f in findings if f.code in select]

    for f in findings:
        print(f.format())

    if findings:
        print(f"\n{len(findings)} finding(s).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
