"""AST-based static checks for asyncio footguns.

Four rules, chosen to match the four footguns named in the loose-end item:

  NGA001  time.sleep(...) called inside an `async def` — stalls the whole event loop
          (and every NiceGUI client sharing it) instead of yielding control.
  NGA002  A known-blocking sync I/O call (requests.*, urllib.request.urlopen,
          subprocess.run/call/check_call/check_output, os.system, builtin open(),
          builtin input()) made inside an `async def`.
  NGA003  `asyncio.create_task(...)` / `asyncio.ensure_future(...)` used as a bare
          expression statement — the returned Task's only strong reference is
          discarded immediately, so asyncio may garbage-collect it mid-flight
          (this is explicitly called out in the asyncio docs: "Save a reference
          to the result of this function, to avoid a task disappearing mid-execution").
  NGA004  A call to a function defined in the same file as `async def` (or an
          `async def` method called as `self.method(...)`), used as a bare
          expression statement without `await` — the coroutine object is created
          and then silently discarded without ever running.

Scope / known limitations (documented, not hidden):
  * Name resolution is intentionally simple and single-file: NGA004 only catches
    calls to async functions/methods *defined in the same source file*. It cannot
    see across imports, so `await`-forgetting on an imported coroutine is not
    caught. This trades recall for zero false positives on ordinary sync calls.
  * NGA002's call list is a fixed, conservative set of well-known blocking APIs,
    not a type-checker — arbitrary blocking third-party calls are not caught.
  * A call already wrapped in `asyncio.create_task(...)` / `asyncio.gather(...)`
    is not re-flagged by NGA004 (the coroutine *is* being scheduled there).
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Iterable, Iterator

RULES: dict[str, str] = {
    "NGA001": "blocking time.sleep() inside an async def — use `await asyncio.sleep(...)` instead",
    "NGA002": "blocking sync I/O call inside an async def — stalls the event loop for every client",
    "NGA003": "asyncio.create_task()/ensure_future() result discarded — task may be GC'd mid-flight; keep a reference",
    "NGA004": "call to a local async function/method without `await` — the coroutine is created and never runs",
}

# dotted-name -> human hint appended to the NGA002 message
_BLOCKING_CALLS: dict[str, str] = {
    "time.sleep": None,  # handled by NGA001 instead
    "requests.get": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.post": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.put": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.delete": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.head": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.patch": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "requests.request": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "urllib.request.urlopen": "use an async HTTP client (httpx.AsyncClient, aiohttp) or run_in_executor",
    "subprocess.run": "use asyncio.create_subprocess_exec/shell, or wrap in run_in_executor",
    "subprocess.call": "use asyncio.create_subprocess_exec/shell, or wrap in run_in_executor",
    "subprocess.check_call": "use asyncio.create_subprocess_exec/shell, or wrap in run_in_executor",
    "subprocess.check_output": "use asyncio.create_subprocess_exec/shell, or wrap in run_in_executor",
    "os.system": "use asyncio.create_subprocess_shell instead",
    "open": "large/slow file I/O inside a coroutine blocks the loop; use aiofiles or run_in_executor",
    "input": "input() blocks the whole event loop waiting on stdin",
    "time.sleep_ms": None,
}

_TASK_SCHEDULERS = {"asyncio.create_task", "create_task", "asyncio.ensure_future", "ensure_future"}


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int
    code: str
    message: str

    def format(self) -> str:
        return f"{self.path}:{self.line}:{self.col}: {self.code} {self.message}"


def _dotted_name(node: ast.AST) -> str | None:
    """Best-effort reconstruction of a dotted call target, e.g. `requests.get`."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


class _AsyncFootgunVisitor(ast.NodeVisitor):
    def __init__(self, path: str, source_lines: list[str]):
        self.path = path
        self.source_lines = source_lines
        self.findings: list[Finding] = []
        self._func_stack: list[bool] = []  # True while inside an `async def`
        # Flat, single-file, best-effort symbol tables built in a pre-pass.
        self.async_func_names: set[str] = set()
        self.async_method_names: set[str] = set()

    # -- pre-pass: collect names of every async def / async method in the file --
    def collect_async_defs(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                self.async_func_names.add(node.name)
                self.async_method_names.add(node.name)

    # -- function scope tracking --
    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func_stack.append(True)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(False)
        self.generic_visit(node)
        self._func_stack.pop()

    # Lambdas can't be `async def`; treat as non-async context so a lambda body
    # doesn't inherit "we're inside an async function" from its enclosing scope
    # for the blocking-call check (a lambda itself never yields regardless).
    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._func_stack.append(False)
        self.generic_visit(node)
        self._func_stack.pop()

    @property
    def _in_async(self) -> bool:
        return bool(self._func_stack) and self._func_stack[-1]

    def _add(self, node: ast.AST, code: str, message: str) -> None:
        self.findings.append(
            Finding(path=self.path, line=node.lineno, col=node.col_offset, code=code, message=message)
        )

    # -- NGA001 / NGA002: blocking calls anywhere inside an async def --
    def visit_Call(self, node: ast.Call) -> None:
        if self._in_async:
            dotted = _dotted_name(node.func)
            if dotted == "time.sleep" or (isinstance(node.func, ast.Attribute) and node.func.attr == "sleep"
                                           and _dotted_name(node.func.value) == "time"):
                self._add(node, "NGA001", RULES["NGA001"])
            elif dotted in _BLOCKING_CALLS and _BLOCKING_CALLS[dotted]:
                self._add(node, "NGA002", f"{RULES['NGA002']} ({dotted}) — {_BLOCKING_CALLS[dotted]}")
        self.generic_visit(node)

    # -- NGA003 / NGA004: bare expression-statement calls --
    def visit_Expr(self, node: ast.Expr) -> None:
        value = node.value
        if isinstance(value, ast.Call):
            dotted = _dotted_name(value.func)
            if dotted in _TASK_SCHEDULERS:
                self._add(
                    node,
                    "NGA003",
                    f"{RULES['NGA003']} ({dotted}(...) return value discarded)",
                )
            elif isinstance(value.func, ast.Name) and value.func.id in self.async_func_names:
                self._add(node, "NGA004", f"{RULES['NGA004']} (`{value.func.id}(...)` never awaited)")
            elif (
                isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id == "self"
                and value.func.attr in self.async_method_names
            ):
                self._add(node, "NGA004", f"{RULES['NGA004']} (`self.{value.func.attr}(...)` never awaited)")
        self.generic_visit(node)


def check_source(source: str, path: str = "<string>") -> list[Finding]:
    """Run all checks over a single module's source text; returns sorted Findings."""
    tree = ast.parse(source, filename=path)
    lines = source.splitlines()
    visitor = _AsyncFootgunVisitor(path, lines)
    visitor.collect_async_defs(tree)
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda f: (f.line, f.col, f.code))


def check_paths(paths: Iterable[Path]) -> Iterator[Finding]:
    """Run checks over every .py file under the given paths (files or directories)."""
    for p in paths:
        p = Path(p)
        files = [p] if p.is_file() else sorted(p.rglob("*.py"))
        for f in files:
            try:
                source = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            try:
                yield from check_source(source, path=str(f))
            except SyntaxError:
                continue
