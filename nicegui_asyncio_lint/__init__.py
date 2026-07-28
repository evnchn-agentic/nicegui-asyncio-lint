"""nicegui_asyncio_lint — static analysis for common asyncio footguns in NiceGUI (and any
asyncio) code: blocking sleeps, blocking sync I/O inside coroutines, fire-and-forget
``asyncio.create_task`` calls whose Task object is never retained, and calls to a
locally-defined ``async def`` that are never awaited.

See README.md for rationale, usage, and known limitations.
"""

from .checker import DEFAULT_EXCLUDE_DIRS, Finding, RULES, check_source, check_paths, iter_python_files

__all__ = ["DEFAULT_EXCLUDE_DIRS", "Finding", "RULES", "check_source", "check_paths", "iter_python_files"]
__version__ = "0.1.0"
