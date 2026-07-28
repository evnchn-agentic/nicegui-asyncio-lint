---
name: nicegui-asyncio-lint
description: Use when reviewing or writing NiceGUI (or any asyncio) Python code and you want to catch blocking-event-loop footguns before they ship — blocking time.sleep() or sync I/O (requests/subprocess/os.system/open/input) inside an async def, asyncio.create_task() calls whose result is discarded (task can be GC'd mid-flight), and calls to a locally-defined async function/method made without await (coroutine silently never runs). Triggers on "check for blocking calls", "asyncio footgun", "why isn't my coroutine running", "event loop is freezing / UI is unresponsive under load", or before merging any NiceGUI page handler / background task.
---

# nicegui-asyncio-lint

A small, dependency-free AST checker (stdlib `ast` only) for four asyncio footguns. Lives
alongside this file as `nicegui_asyncio_lint/` — a normal importable Python package plus a CLI.

## When to reach for this

- Reviewing a diff that touches an `async def` NiceGUI page handler, `@ui.refreshable`, timer
  callback, or WebSocket handler.
- A NiceGUI app reports "the whole UI freezes when one client does X" — that's the canonical
  symptom of `NGA001`/`NGA002` (one blocking call in a shared event loop stalls every client).
- A background task "just doesn't seem to run" or silently stops partway — check for `NGA003`
  (task GC'd because nothing held a reference) or `NGA004` (never awaited in the first place).

## How to run it

```bash
cd <this skill's directory>
source .venv/bin/activate   # created via: uv venv .venv && uv pip install -e ".[test]"
nicegui-asyncio-lint <path-to-file-or-project>
```

Pointing it at a project root is safe: dot-directories and vendored trees (`.venv`, `venv`,
`node_modules`, `site-packages`, …) are pruned, so you only see findings in code you own. Add
names with `--exclude a,b`; scan everything with `--no-default-excludes`.

Or import directly without the CLI:

```python
from nicegui_asyncio_lint import check_source
findings = check_source(open("app.py").read(), path="app.py")
```

## Reading the output

Each line is `path:line:col: CODE message`. `NGA00{1,2}` fixes are "make it actually async"
(swap for `asyncio.sleep`, an async HTTP client, `asyncio.create_subprocess_*`, `aiofiles`, or
wrap the sync call in `loop.run_in_executor(...)`). `NGA003` fixes are "keep the Task": assign
it to a variable, `await` it, or stash it in a module-level `set` of in-flight tasks with a
`add_done_callback` to discard on completion. `NGA004` fixes are "add the missing `await`".

## Known false-positive class — read before trusting a `NGA004` hit

If the same source file defines **multiple different functions with the same name in different
local scopes**, and *any one* of those is `async def`, a bare call to a *different*, non-async,
same-named function elsewhere in the file can be misflagged as `NGA004`. This is a real,
demonstrated limitation (see `README.md`'s "Known limitations" section for the exact case it
was caught on) — always read the flagged line before assuming the finding is correct; it is a
lead to check, not a verdict.

## Full detail

See `README.md` in this directory for the rule table, limitations, self-verification log, and
the packaging options (standalone pip tool vs. NiceGUI contrib PR).
