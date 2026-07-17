"""AST-based static checks for asyncio footguns.

Four rules, chosen to match the four footguns named in the loose-end item:

  NGA001  time.sleep(...) called inside an `async def` — stalls the whole event loop
          (and every NiceGUI client sharing it) instead of yielding control.
  NGA002  A known-blocking sync I/O call (requests.*, urllib.request.urlopen,
          subprocess.run/call/check_call/check_output, os.system, builtin input())
          made inside an `async def`.
  NGA003  `asyncio.create_task(...)` / `asyncio.ensure_future(...)` used as a bare
          expression statement — the returned Task's only strong reference is
          discarded immediately, so asyncio may garbage-collect it mid-flight
          (the asyncio docs: "Save a reference to the result of this function,
          to avoid a task disappearing mid-execution").
  NGA004  A call to a function/method defined *async* in this same file, used
          without `await` as a bare expression statement OR as a bare `return`
          value — the coroutine object is created and then discarded/returned
          instead of run.

Design notes (what makes it low-false-positive):
  * Import-aware, single file. A small pre-pass reads `import`/`from ... import`
    so `from time import sleep; sleep(1)`, `import requests as r; r.get(...)`,
    and `from requests import get; get(...)` are all resolved to their canonical
    dotted name before matching. Arbitrary blocking third-party calls are still
    not caught (this is a curated list, not a type-checker).
  * Local shadowing is respected: a *parameter* named `time`/`open`/`requests`
    etc. shadows the module, so `async def f(time): time.sleep(1)` is NOT flagged.
  * NGA004 only fires on names that are async in this file AND never also defined
    as a plain `def` (ambiguous names are skipped), so an ordinary sync
    `self.refresh()` is not flagged just because some other class has an async
    `refresh`. Cross-import await-forgetting is out of scope (single-file only).
  * builtin `open()` is intentionally NOT in the default blocking set — it is too
    common (small config reads) to fail CI on by default; add it yourself if you
    want it. `input()` stays (it hard-blocks the loop on stdin and is rare).

Known limitations (documented, not hidden):
  * NGA003/NGA004 catch the discarded-as-a-statement and returned forms, but not
    "assigned to a local that is then never used" (that needs use/flow analysis).
  * Decorator expressions are evaluated at import time, so they are checked in the
    *enclosing* scope, not as if inside the coroutine body.
"""
from __future__ import annotations

import ast
import dataclasses
import sys
from pathlib import Path
from typing import Iterable, Iterator

RULES: dict[str, str] = {
    "NGA001": "blocking time.sleep() inside an async def — use `await asyncio.sleep(...)` instead",
    "NGA002": "blocking sync I/O call inside an async def — stalls the event loop for every client",
    "NGA003": "asyncio.create_task()/ensure_future() result discarded — task may be GC'd mid-flight; keep a reference",
    "NGA004": "call to a local async function/method without `await` — the coroutine is created and never runs",
}

# canonical dotted-name -> human hint appended to the NGA002 message.
# NB: builtin open() is deliberately excluded from the default set (too noisy).
_BLOCKING_CALLS: dict[str, str] = {
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
    "input": "input() blocks the whole event loop waiting on stdin",
}

_SLEEP = "time.sleep"
_TASK_SCHEDULERS = {"asyncio.create_task", "asyncio.ensure_future"}


@dataclasses.dataclass(frozen=True)
class Finding:
    path: str
    line: int
    col: int  # 1-based (matches flake8/ruff/pyflakes convention)
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


class _Imports:
    """Resolves a syntactic dotted call target to a canonical module path,
    following single-file `import` / `from ... import` bindings."""

    def __init__(self, tree: ast.AST) -> None:
        self.module_alias: dict[str, str] = {}  # local root -> real module (import x [as y])
        self.name_alias: dict[str, str] = {}     # local name -> canonical dotted (from m import n)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.module_alias[a.asname] = a.name
                    else:
                        # `import a.b.c` binds the root name `a`
                        self.module_alias[a.name.split(".")[0]] = a.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if node.level:  # relative import — can't canonicalise reliably
                    continue
                for a in node.names:
                    local = a.asname or a.name
                    self.name_alias[local] = f"{mod}.{a.name}" if mod else a.name

    def resolve(self, dotted: str | None) -> str | None:
        if dotted is None:
            return None
        parts = dotted.split(".")
        first, rest = parts[0], parts[1:]
        if not rest:  # bare name
            return self.name_alias.get(first, first)
        if first in self.module_alias:
            return ".".join([self.module_alias[first], *rest])
        if first in self.name_alias:  # `from a import b; b.c(...)`
            return ".".join([self.name_alias[first], *rest])
        return dotted


class _AsyncFootgunVisitor(ast.NodeVisitor):
    def __init__(self, path: str, imports: _Imports, async_only: set[str]) -> None:
        self.path = path
        self.imports = imports
        self.async_only = async_only  # names that are async def AND never plain def
        self.findings: list[Finding] = []
        self._async_stack: list[bool] = []
        self._locals_stack: list[set[str]] = []  # bound names (params) shadowing modules

    # -- scope handling --------------------------------------------------------
    def _params(self, args: ast.arguments) -> set[str]:
        names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
        if args.vararg:
            names.add(args.vararg.arg)
        if args.kwarg:
            names.add(args.kwarg.arg)
        return names

    def _enter(self, node, is_async: bool) -> None:
        # decorators run at import time in the ENCLOSING scope — visit them first
        for dec in getattr(node, "decorator_list", []):
            self.visit(dec)
        self._async_stack.append(is_async)
        self._locals_stack.append(self._params(node.args))
        for stmt in node.body:
            self.visit(stmt)
        self._async_stack.pop()
        self._locals_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._enter(node, True)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enter(node, False)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # a lambda never yields; its body is not an async context
        self._async_stack.append(False)
        self._locals_stack.append(self._params(node.args))
        self.visit(node.body)
        self._async_stack.pop()
        self._locals_stack.pop()

    @property
    def _in_async(self) -> bool:
        return bool(self._async_stack) and self._async_stack[-1]

    def _is_local(self, dotted: str | None) -> bool:
        if not dotted:
            return False
        root = dotted.split(".")[0]
        return any(root in scope for scope in self._locals_stack)

    def _add(self, node: ast.AST, code: str, message: str) -> None:
        line = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", -1) + 1  # 1-based
        self.findings.append(Finding(self.path, line, col, code, message))

    # -- NGA001 / NGA002: blocking calls anywhere inside an async def ----------
    def visit_Call(self, node: ast.Call) -> None:
        if self._in_async:
            raw = _dotted_name(node.func)
            if not self._is_local(raw):
                canon = self.imports.resolve(raw)
                if canon == _SLEEP:
                    self._add(node, "NGA001", RULES["NGA001"])
                elif canon in _BLOCKING_CALLS:
                    self._add(node, "NGA002", f"{RULES['NGA002']} ({canon}) — {_BLOCKING_CALLS[canon]}")
        self.generic_visit(node)

    # -- NGA003 / NGA004 --------------------------------------------------------
    def _check_discarded_call(self, node: ast.AST, value: ast.AST, form: str) -> None:
        if not isinstance(value, ast.Call):
            return
        func = value.func
        raw = _dotted_name(func)
        canon = self.imports.resolve(raw)
        if canon in _TASK_SCHEDULERS and not self._is_local(raw):
            self._add(node, "NGA003", f"{RULES['NGA003']} ({canon}(...) {form})")
            return
        # NGA004: a bare local-async name (skip if a param shadows it), or
        # self.method where the method name is async-only (self is expected to be
        # a param here, so the shadow guard must NOT apply to this branch).
        if isinstance(func, ast.Name) and func.id in self.async_only and not self._is_local(func.id):
            self._add(node, "NGA004", f"{RULES['NGA004']} (`{func.id}(...)` {form}, never awaited)")
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and func.attr in self.async_only
        ):
            self._add(node, "NGA004", f"{RULES['NGA004']} (`self.{func.attr}(...)` {form}, never awaited)")

    def visit_Expr(self, node: ast.Expr) -> None:
        self._check_discarded_call(node, node.value, "return value discarded")
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is not None:
            self._check_discarded_call(node, node.value, "returned without await")
        self.generic_visit(node)


def _async_only_names(tree: ast.AST) -> set[str]:
    """Names defined as `async def` somewhere and never also as a plain `def`."""
    async_names: set[str] = set()
    sync_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            async_names.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            sync_names.add(node.name)
    return async_names - sync_names


def check_source(source: str, path: str = "<string>") -> list[Finding]:
    """Run all checks over a single module's source text; returns sorted Findings."""
    tree = ast.parse(source, filename=path)
    visitor = _AsyncFootgunVisitor(path, _Imports(tree), _async_only_names(tree))
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda f: (f.line, f.col, f.code))


def check_paths(paths: Iterable[Path]) -> Iterator[Finding]:
    """Run checks over every .py file under the given paths (files or directories).

    `.pyi` stubs are skipped (they contain no runtime code). Files that fail to
    parse are reported on stderr (not silently swallowed) and skipped."""
    for p in paths:
        p = Path(p)
        if p.is_file():
            files = [p]
        else:
            files = sorted(f for f in p.rglob("*.py") if f.suffix == ".py")
        for f in files:
            if f.suffix == ".pyi":
                continue
            try:
                source = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError) as exc:
                print(f"{f}: skipped (unreadable: {exc})", file=sys.stderr)
                continue
            try:
                yield from check_source(source, path=str(f))
            except SyntaxError as exc:
                print(f"{f}: skipped (syntax error: {exc})", file=sys.stderr)
                continue
