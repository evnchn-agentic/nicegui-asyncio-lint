"""CLI entry point: `nicegui-asyncio-lint <file-or-dir> [...]`.

Exit code is 1 if any finding was reported (CI-friendly), 0 otherwise.
Usable directly, or as a pre-commit local hook (see README.md).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checker import DEFAULT_EXCLUDE_DIRS, check_paths


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
    parser.add_argument(
        "--exclude",
        default=None,
        help="comma-separated extra directory NAMES to prune while walking, e.g. migrations,vendor",
    )
    parser.add_argument(
        "--no-default-excludes",
        action="store_true",
        help=f"also scan dot-directories and {', '.join(sorted(DEFAULT_EXCLUDE_DIRS))} "
        "(by default those are pruned, so a project's .venv is not linted)",
    )
    args = parser.parse_args(argv)

    select = set(args.select.split(",")) if args.select else None
    extra = {n.strip() for n in args.exclude.split(",") if n.strip()} if args.exclude else set()
    exclude_dirs = extra if args.no_default_excludes else DEFAULT_EXCLUDE_DIRS | extra

    findings = list(check_paths(
        args.paths, exclude_dirs=exclude_dirs, skip_dot_dirs=not args.no_default_excludes))
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
