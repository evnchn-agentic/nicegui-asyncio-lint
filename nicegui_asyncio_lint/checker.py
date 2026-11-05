"""AST-based static checks for asyncio footguns.

Four rules, chosen to match the four footguns named in the loose-end item:

  NGA001  time.sleep(...) called inside an `async def` — stalls the whole event loop
          (and every NiceGUI client sharing it) instead of yielding control.
  NGA002  A known-blocking sync I/O call (requests.*, urllib.request.urlopen,
          subprocess.run/call/check_call/check_output, os.system, builtin input())
          made inside an `async def`.
  NGA001/NGA002 also fire in sync functions NiceGUI runs on the event loop: `@ui.page`
          functions, `on_*=` handlers, `ui.timer`/`app.timer` callbacks, and same-file sync
          functions those call directly (files importing nicegui only), and in any file in
          the same-file sync functions an `async def` calls directly.
  NGA003  `asyncio.create_task(...)` / `asyncio.ensure_future(...)` used as a bare
          expression statement — the returned Task's only strong reference is
          discarded immediately, so asyncio may garbage-collect it mid-flight
          (the asyncio docs: "Save a reference to the result of this function,
          to avoid a task disappearing mid-execution").
  NGA004  A call to a function/method defined *async* in this same file, used
          without `await` as a bare expression statement OR as the `return`
          value of another `async def` — the coroutine object is created and then
          discarded/returned instead of run. Async generators are exempt (calling
          one returns an async iterator, which is never awaited), and so is
          `return coro()` from a plain `def`, which hands the awaitable to its caller.

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
  * NGA003 catches the discarded-as-a-statement form only: a *returned* Task hands
    its reference to the caller.
  * NGA003/NGA004 do not catch
    "assigned to a local that is then never used" (that needs use/flow analysis).
  * Decorator expressions are evaluated at import time, so they are checked in the
    *enclosing* scope, not as if inside the coroutine body.
"""
from __future__ import annotations

import ast
import dataclasses
import os
import sys
from pathlib import Path
from typing import Collection, Iterable, Iterator

#: Directory *names* skipped when walking a directory, on top of the
#: skip-dot-directories rule (which covers .venv/.git/.tox/.mypy_cache/...).
#: A path named explicitly on the command line is never excluded.
#: The non-dot names are the subset of ruff's default exclude list that can hold .py files.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    "venv", "node_modules", "__pycache__", "__pypackages__", "site-packages", "dist", "_build", "buck-out",
})

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
_PAGE_DECORATORS = {"nicegui.ui.page", "ui.page"}
_TIMERS = {"nicegui.ui.timer", "ui.timer", "nicegui.app.timer", "app.timer"}
_HANDLER_CONTEXT = "in a sync NiceGUI page or event handler, which runs on the event loop"
_FROM_ASYNC_CONTEXT = "in a sync function called from an async def, so it runs on the event loop"
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
    def __init__(self, path: str, imports: _Imports, async_only: set[str], on_loop: dict[int, str]) -> None:
        self.path = path
        self.imports = imports
        self.async_only = async_only  # names that are async def AND never plain def
        self.on_loop = on_loop  # id of each sync def/lambda run on the event loop -> why
        self.findings: list[Finding] = []
        self._async_stack: list[str] = []  # "async", an on-loop context, or "" per enclosing function
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
        ctx = "async" if is_async else self.on_loop.get(id(node), "")
        # decorators run at import time in the ENCLOSING scope — visit them first
        for dec in getattr(node, "decorator_list", []):
            self.visit(dec)
        self._async_stack.append(ctx)
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
        self._async_stack.append(self.on_loop.get(id(node), ""))
        self._locals_stack.append(self._params(node.args))
        self.visit(node.body)
        self._async_stack.pop()
        self._locals_stack.pop()

    @property
    def _in_async(self) -> bool:
        return bool(self._async_stack) and self._async_stack[-1] == "async"

    @property
    def _in_handler(self) -> bool:
        return bool(self._async_stack) and self._async_stack[-1] not in ("", "async")

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
        if self._in_async or self._in_handler:
            raw = _dotted_name(node.func)
            if not self._is_local(raw):
                canon = self.imports.resolve(raw)
                if canon == _SLEEP:
                    self._add(node, "NGA001", f"blocking time.sleep() {self._async_stack[-1]} — make it `async def` "
                              "and `await asyncio.sleep(...)`" if self._in_handler else RULES["NGA001"])
                elif canon in _BLOCKING_CALLS:
                    msg = RULES["NGA002"].replace("inside an async def", self._async_stack[-1]) if self._in_handler \
                        else RULES["NGA002"]
                    self._add(node, "NGA002", f"{msg} ({canon}) — {_BLOCKING_CALLS[canon]}")
        self.generic_visit(node)

    # -- NGA003 / NGA004 --------------------------------------------------------
    def _check_discarded_call(self, node: ast.AST, value: ast.AST, form: str) -> None:
        if not isinstance(value, ast.Call):
            return
        func = value.func
        raw = _dotted_name(func)
        canon = self.imports.resolve(raw)
        if canon in _TASK_SCHEDULERS and not self._is_local(raw):
            if not isinstance(node, ast.Return):  # a returned Task is referenced by the caller
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
        # only an `async def` returning a coroutine is a forgotten await; a plain
        # `def` returning one hands the awaitable to its caller
        if node.value is not None and self._in_async:
            self._check_discarded_call(node, node.value, "returned without await")
        self.generic_visit(node)


def _is_generator(node: ast.AST) -> bool:
    """True if the function's own body (not a nested scope) yields."""
    todo = list(node.body)
    while todo:
        child = todo.pop()
        if isinstance(child, (ast.Yield, ast.YieldFrom)):
            return True
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            todo.extend(ast.iter_child_nodes(child))
    return False


def _async_only_names(tree: ast.AST) -> set[str]:
    """Names defined as a coroutine `async def` somewhere and never also as a plain
    `def` or an async generator."""
    async_names: set[str] = set()
    sync_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and not _is_generator(node):
            async_names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            sync_names.add(node.name)
    return async_names - sync_names


def _direct_calls(fn: ast.AST) -> Iterator[ast.Call]:
    """Calls in fn's own body: nested defs and lambdas are not run by calling fn."""
    body = list(fn.body) if isinstance(fn.body, list) else [fn.body]
    while body:
        child = body.pop()
        if isinstance(child, ast.Call):
            yield child
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            body.extend(ast.iter_child_nodes(child))


def _on_loop_sync_callables(tree: ast.AST, imports: _Imports) -> dict[int, str]:
    """Maps the id of each sync def and lambda that runs on the event loop to the reason: same-file
    sync functions an `async def` calls directly (any file), and, in files importing nicegui,
    `@ui.page` functions and callables passed as `on_*=` handlers or `ui.timer`/`app.timer`
    callbacks. The sync functions those call directly count too. A function named by `name` or
    `self.name` counts only when the file defines exactly one function of that name."""
    defs: dict[str, list[ast.AST]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.setdefault(node.name, []).append(node)

    def target(v: ast.AST) -> ast.AST | None:
        if isinstance(v, ast.Call) and imports.resolve(_dotted_name(v.func)) == "functools.partial" and v.args:
            v = v.args[0]
        if isinstance(v, ast.Lambda):
            return v
        name = v.id if isinstance(v, ast.Name) else (
            v.attr if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == "self" else None)
        fn = defs[name][0] if name and len(defs.get(name, ())) == 1 else None
        if isinstance(fn, ast.FunctionDef) and not _is_generator(fn):  # calling a generator runs nothing
            return fn
        return None

    def called(fn: ast.AST) -> list[ast.AST | None]:
        a = fn.args  # a parameter of the same name is whatever the caller passed, not the def
        params = {p.arg for p in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if p}
        return [None if isinstance(c.func, ast.Lambda) or (isinstance(c.func, ast.Name) and c.func.id in params)
                else target(c.func) for c in _direct_calls(fn)]

    # a sync function an async def calls directly runs on the loop: pure asyncio, no nicegui needed
    todo: list[tuple[ast.AST | None, str]] = [
        (fn, _FROM_ASYNC_CONTEXT)
        for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) for fn in called(node)]
    roots = {m.split(".")[0] for m in (*imports.module_alias.values(), *imports.name_alias.values())}
    # roots of other imported libraries: their on_*= callbacks may run in their own threads (paho-mqtt)
    foreign = roots - {"nicegui"}
    handlers: list[ast.AST | None] = []
    for node in ast.walk(tree) if "nicegui" in roots else ():
        if isinstance(node, ast.FunctionDef):
            for dec in node.decorator_list:
                if imports.resolve(_dotted_name(dec.func if isinstance(dec, ast.Call) else dec)) in _PAGE_DECORATORS:
                    handlers.append(node)
        elif isinstance(node, ast.Call):
            canon = imports.resolve(_dotted_name(node.func)) or ""
            if canon.split(".")[0] not in foreign:
                handlers += [target(kw.value) for kw in node.keywords if (kw.arg or "").startswith("on_")]
            if canon in _TIMERS:
                handlers += [target(v) for v in node.args[1:2]] + [target(kw.value) for kw in node.keywords if kw.arg == "callback"]
    todo += [(fn, _HANDLER_CONTEXT) for fn in handlers]  # popped first, so a handler keeps the handler message
    found: dict[int, str] = {}
    while todo:
        fn, why = todo.pop()
        if fn is None or id(fn) in found:
            continue
        found[id(fn)] = why
        todo += [(f, why) for f in called(fn)]
    return found


def check_source(source: str, path: str = "<string>") -> list[Finding]:
    """Run all checks over a single module's source text; returns sorted Findings."""
    tree = ast.parse(source, filename=path)
    imports = _Imports(tree)
    visitor = _AsyncFootgunVisitor(path, imports, _async_only_names(tree), _on_loop_sync_callables(tree, imports))
    visitor.visit(tree)
    return sorted(visitor.findings, key=lambda f: (f.line, f.col, f.code))


def _report_walk_error(exc: OSError) -> None:
    print(f"{exc.filename}: skipped (unreadable: {exc})", file=sys.stderr)


def iter_python_files(
    root: Path,
    *,
    exclude_dirs: Collection[str] = DEFAULT_EXCLUDE_DIRS,
    skip_dot_dirs: bool = True,
) -> Iterator[Path]:
    """Yield every `.py` file under `root`, pruning excluded directories.

    A directory is pruned (never descended into) when its name is in
    `exclude_dirs`, or — with `skip_dot_dirs` — when it starts with a dot. Only
    *discovered* directories are pruned: `root` itself is always scanned, so an
    excluded path can still be linted by naming it explicitly (a whole venv needs
    `exclude_dirs=()` too, since `site-packages` is excluded by name).
    `.pyi` stubs never match (they contain no runtime code). Paths come out
    globally sorted, and an unreadable directory is reported on stderr rather
    than silently skipped."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_report_walk_error):
        dirnames[:] = [
            d for d in dirnames
            if d not in exclude_dirs and not (skip_dot_dirs and d.startswith("."))
        ]
        found.extend(Path(dirpath) / name for name in filenames if name.endswith(".py"))
    yield from sorted(found)


def check_paths(
    paths: Iterable[Path],
    *,
    exclude_dirs: Collection[str] = DEFAULT_EXCLUDE_DIRS,
    skip_dot_dirs: bool = True,
) -> Iterator[Finding]:
    """Run checks over every .py file under the given paths (files or directories).

    Virtualenvs and vendored trees are pruned by default (see `iter_python_files`)
    — without that, linting a project root reports findings from third-party code
    the user cannot fix, and exits non-zero in CI. Files that fail to parse are
    reported on stderr (not silently swallowed) and skipped."""
    for p in paths:
        p = Path(p)
        if p.is_file():
            files: Iterable[Path] = [p]
        else:
            files = iter_python_files(p, exclude_dirs=exclude_dirs, skip_dot_dirs=skip_dot_dirs)
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
