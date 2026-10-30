# nicegui-asyncio-lint

Static-analysis checker for four common asyncio footguns — built because these bite NiceGUI
apps particularly hard (one blocking call in a page handler stalls the event loop for *every*
connected client, not just the caller), but the checks are generic asyncio, not NiceGUI-specific.

> Loose end #6 (`agent-asyncio footgun linter for NiceGUI`, conv #363, 2026-06-02): *"I'll roll
> this into its own artifact later, useful for NiceGUI even."* This is that artifact.

## The four rules

| Code | Catches | Example |
|---|---|---|
| `NGA001` | `time.sleep(...)` inside `async def` | `async def f(): time.sleep(2)` |
| `NGA002` | Known-blocking sync I/O inside `async def` — `requests.*`, `urllib.request.urlopen`, `subprocess.run/call/check_call/check_output`, `os.system`, builtin `input()` (builtin `open()` is **not** in the default set — too common to fail CI on) | `async def f(): requests.get(url)` |
| `NGA003` | `asyncio.create_task()` / `asyncio.ensure_future()` whose return value is discarded (not assigned to anything) | `async def f(): asyncio.create_task(bg())` |
| `NGA004` | A call to a function/method defined `async def` in the same file, used as a bare statement without `await` | `async def f(): some_local_coro()` |

`NGA001` and `NGA002` also fire in the **sync** functions NiceGUI runs on the event loop: a sync
`@ui.page` function, a callable passed as an `on_*=` handler or as a `ui.timer`/`app.timer`
callback, and any same-file sync function those call directly. NiceGUI calls all of these inline
on the loop (`page.py` wraps the page function in an `async def`; `events.handle_event` calls the
handler), so `ui.button(on_click=save)` with a `time.sleep` in `save` freezes every client just
like the `async def` case. Measured on NiceGUI 3.18: a 1.5 s `time.sleep` in a sync page or a
sync `on_click` handler stalled a 50 ms heartbeat coroutine for 1.69 s. ruff's `ASYNC2xx` rules
only look inside `async def`, so they miss this. This check is on only in files that import
`nicegui`; `run.io_bound(save)` is not flagged, because it passes `save` rather than calling it.

`NGA003` and `NGA004` are two faces of the same asyncio trap class the CPython docs warn about
explicitly: a coroutine or Task object that's created and then has no strong reference held to
it. For `NGA004` the coroutine object is simply never scheduled at all (silently does nothing —
no error, no warning, just a `RuntimeWarning: coroutine '...' was never awaited` if you're lucky
enough to have warnings visible). For `NGA003` the Task *is* scheduled, but asyncio only holds a
*weak* reference to it once you drop the strong one, so it can be garbage-collected mid-flight —
see the "Important" box in the [`asyncio.create_task` docs](https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task).

## Install & run

```bash
cd nicegui-asyncio-lint   # this directory
uv venv .venv && source .venv/bin/activate
uv pip install -e ".[test]"

# scan a file or a whole tree
nicegui-asyncio-lint path/to/app.py
nicegui-asyncio-lint path/to/project/

# restrict to specific rules
nicegui-asyncio-lint --select NGA001,NGA003 path/to/project/
```

Exit code is `1` if anything was flagged (CI-friendly), `0` otherwise.

### What gets scanned

Walking a directory prunes dot-directories (`.venv`, `.git`, `.tox`, `.mypy_cache`, …) and the
names in `DEFAULT_EXCLUDE_DIRS` (`venv`, `node_modules`, `__pycache__`, `__pypackages__`,
`site-packages`, `dist`, `_build`, `buck-out`) — the subset of ruff's default exclude list that
can hold `.py` files. Without this, `nicegui-asyncio-lint .` on a fresh NiceGUI project reports
findings from `anyio`/`engineio`/`nicegui` inside the project's own `.venv` — third-party code
the user cannot fix — and exits `1`.

Only *discovered* directories are pruned; a path named on the command line is always scanned, so
`nicegui-asyncio-lint .venv/lib/python3.12/site-packages` still works. That is also the escape
hatch for code you *do* own inside a dot-directory — `.github/scripts/` is skipped by the walk
unless you name it.

```bash
nicegui-asyncio-lint --exclude vendor,migrations .   # prune extra directory names
nicegui-asyncio-lint --no-default-excludes .         # scan everything, venv included
```

### As a pre-commit hook

```yaml
# .pre-commit-config.yaml
- repo: local
  hooks:
    - id: nicegui-asyncio-lint
      name: nicegui-asyncio-lint
      entry: nicegui-asyncio-lint
      language: system
      types: [python]
```

### As a library (e.g. from a NiceGUI dev-mode startup check)

```python
from nicegui_asyncio_lint import check_paths

findings = list(check_paths([Path("my_app_dir")]))
for f in findings:
    print(f.format())
```

## Self-verification performed (this session)

- `pytest tests/ -v` → **all passed** (one test per rule's true-positive + true-negative
  boundary, plus a directory-walk + syntax-error-tolerance test; 8 at the time of writing,
  28 today).
- Ran the built CLI against real code on this machine, read-only:
  - `~/nicegui/nicegui` (243 `.py` files, NiceGUI's own core library) → **0 findings** — plausible
    for a mature, reviewed core lib; the checks target app-level mistakes.
  - `~/nicegui` including its `.venv` and test suite (`trio`, `starlette` vendored source +
    NiceGUI's own tests) → **15 findings**, including real, independently-known patterns:
    `trio`'s own test suite intentionally calls blocking `time.sleep()`/`open()` inside
    `async def` test helpers (expected — those are deliberately testing blocking-thread
    behaviour, not app code), and one genuine **false positive** surfaced and is written up
    below because it directly demonstrates the documented scoping limitation.

## Precision on third-party code, and overlap with ruff (2026-10-19)

Run over NiceGUI's dev `.venv` (`--no-default-excludes`: redis, jinja2, engineio, playwright, trio and others) plus NiceGUI's own
source, the checker reported 35 findings. Hand-labelled, 14 were true positives and 21 were false
positives. Three gaps, fixed since, account for 20 of them: a plain `def` returning a coroutine (redis lock
`release()`, jinja2 `Macro.__call__`), a call to an async generator (NiceGUI
`FileUpload.iterate()`, jinja2 `do_select`), and a returned Task (engineio
`start_background_task`). The same corpus now gives the 14 true positives and one false positive.

ruff's `ASYNC251` and `RUF006` found the same `NGA001`/`NGA003` true positives on that corpus,
except three `time.sleep` calls in trio's tests that carry `# noqa: ASYNC251`. If you already run
ruff with those rules, only `NGA004` adds anything, and on this corpus it found no confirmed bug.
Its one remaining hit is redis-py's cluster `TransactionStrategy._execute_command`, an `async def`
that returns `self._immediate_execute_command(...)` unawaited on purpose: while WATCHing, the
caller does `await pipe.get(key)` (redis-py's own tests do this). An AST check cannot tell that
apart from a forgotten `await`.

## Known limitations (found empirically, not just theorized)

- **Single-file, flat, unscoped name resolution for `NGA004`.** The checker does not do real
  scope analysis — it collects every `async def name(...)` in a file into one flat set, then
  flags any bare `name(...)` call as a suspected missing-await. If a file defines **multiple
  functions with the same name in different local scopes** (e.g. several test functions each
  locally defining a differently-implemented `number_ui`), and *any one* of those same-named
  functions is `async def`, a bare call to a *different*, non-async, same-named function
  elsewhere in the file will be misflagged.
  **This is not hypothetical** — it fired for real against `nicegui/tests/test_refreshable.py`,
  which defines `number_ui` three separate times in three separate test functions (lines 11, 39,
  258), only one of which (`line 39`) is `async def`. The checker flagged the unrelated sync
  `number_ui()` call at line 16 as `NGA004`. A real implementation shipped as a NiceGUI contrib
  module or PR should either scope symbol collection per-enclosing-function, or (more robustly)
  keep `NGA004` low-noise, it only fires on names that are `async def` in *this* file **and**
  never also defined as a plain `def` (ambiguous names are skipped), so an ordinary sync
  `self.refresh()` is never flagged just because some other class has an async `refresh`.
- **Import-aware, single file.** `NGA001`/`NGA002` resolve `import x as y`, `from m import n`,
  and `from m import n as p` to the canonical name before matching (so `from time import sleep;
  sleep(1)` and `import requests as r; r.get(...)` are caught), and a *parameter* that shadows a
  module (`async def f(time): time.sleep(1)`) is correctly **not** flagged. Cross-file resolution
  is still out of scope — a missing `await` on an *imported* coroutine is not caught.
- **`NGA003` catches the discarded-as-a-statement form; `NGA004` also catches `return coro()`
  inside another `async def`.** Neither flags a plain `def` returning a coroutine or Task (the
  caller gets the awaitable) or a call to an async generator (it returns an async iterator).
  The trade-off: a sync callback that returns a coroutine to a consumer which discards it
  (`loop.call_soon(cb)`) is no longer caught. The async-generator exemption assumes decorators
  keep the calling convention, and only looks for `yield` in the function's own body (not in a
  nested def's default arguments or decorators, where Python also evaluates it in the outer scope).
  Neither catches "assigned to a local that is then never used"** (`t = create_task(...)` with `t` never
  referenced). That needs use/flow analysis; it's the natural next step, not done here.
- **`NGA002`'s blocking-call list is a fixed, conservative allowlist-of-known-badness**, not a
  type checker. Arbitrary third-party blocking calls (a blocking driver's `.execute()`, a
  synchronous SDK method, etc.) are invisible to it by design — extending the list is the
  natural next step (see TODO below).
- **No cross-function blocking-call propagation.** If `async def a()` calls a *sync* helper
  `def b()` that itself calls `time.sleep()`, only `b`'s body is outside an async-def in the
  AST's eyes and is not flagged, even though `a()` transitively blocks. Full callgraph analysis
  is out of scope for a fast, dependency-free single-pass AST checker.

## Packaging path (per the loose-end's own phrasing: "skill or NiceGUI contrib")

Both are viable from this same code, unmodified:

1. **Standalone pip-installable tool** (what's built here) — `pip install nicegui-asyncio-lint`
   (once actually published), run via CLI or pre-commit, works on any asyncio codebase.
2. **NiceGUI contrib module / PR** — copy `nicegui_asyncio_lint/` into
   `nicegui/testing/asyncio_lint.py` or a new `scripts/asyncio_lint.py` in the NiceGUI repo,
   wire it into `docs`/`CONTRIBUTING.md` as an optional dev-mode check, and/or add a pytest
   marker that runs it over `examples/` in CI to keep NiceGUI's own example gallery footgun-free.
   Given the `NGA004` false-positive found against NiceGUI's *own* test suite above, step 1 of
   any such PR should be tightening `NGA004` scope-resolution first — shipping it against
   NiceGUI's codebase as-is would immediately self-flag.
3. A draft **Claude Code skill wrapper** (`SKILL.md` in this directory) is included as a third
   option so an agent session can reach for this tool by trigger phrase without a human first
   publishing it anywhere.

## TODO (left for the human / next session, see `remaining_for_human`)

- [ ] Fix the `NGA004` false positive class (scope-qualify the symbol table by enclosing
      function, not just by file) — or ship it as a lower-severity warning until fixed.
- [ ] Decide: publish to PyPI standalone, or open a NiceGUI PR (or both).
- [ ] If NiceGUI PR: pick the actual target path/module name inside the NiceGUI repo, write it
      as a `git worktree` branch (`wt new <branch>`), add it to CI, run it over `examples/`.
- [ ] Expand the `NGA002` blocking-call table based on real NiceGUI-app postmortems if any
      surface (this list was built from general Python-asyncio knowledge, not a NiceGUI-specific
      incident corpus).
