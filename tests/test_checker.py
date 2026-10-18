from pathlib import Path

from nicegui_asyncio_lint.checker import DEFAULT_EXCLUDE_DIRS, check_source, check_paths
from nicegui_asyncio_lint.cli import main

FIXTURES = Path(__file__).parent / "fixtures"


def _codes(findings):
    return sorted(f.code for f in findings)


def test_bad_examples_flags_all_four_rules():
    source = (FIXTURES / "bad_examples.py").read_text()
    findings = check_source(source, path="bad_examples.py")
    codes = _codes(findings)
    assert "NGA001" in codes
    assert "NGA002" in codes
    assert "NGA003" in codes
    assert "NGA004" in codes
    # exactly the expected count: 1 sleep + 3 blocking-io + 1 fire-and-forget + 2 missing-await
    assert len(findings) == 7, findings


def test_good_examples_is_clean():
    source = (FIXTURES / "good_examples.py").read_text()
    findings = check_source(source, path="good_examples.py")
    assert findings == [], [f.format() for f in findings]


def test_nga001_blocking_sleep_message_mentions_asyncio_sleep():
    source = "async def f():\n    import time\n    time.sleep(1)\n"
    findings = check_source(source)
    assert len(findings) == 1
    assert findings[0].code == "NGA001"
    assert "asyncio.sleep" in findings[0].message


def test_nga002_sync_sleep_outside_async_def_not_flagged():
    source = "import time\n\ndef f():\n    time.sleep(1)\n"
    assert check_source(source) == []


def test_nga003_create_task_result_used_is_not_flagged():
    source = (
        "import asyncio\n\n"
        "async def helper():\n    await asyncio.sleep(0)\n\n"
        "async def f():\n    t = asyncio.create_task(helper())\n    await t\n"
    )
    findings = check_source(source)
    assert findings == []


def test_nga004_call_wrapped_in_create_task_is_not_double_flagged():
    source = (
        "import asyncio\n\n"
        "async def helper():\n    await asyncio.sleep(0)\n\n"
        "async def f():\n    asyncio.create_task(helper())\n"
    )
    findings = check_source(source)
    # only the discarded-task footgun (NGA003) should fire; helper() is an
    # *argument* to create_task, not a bare expression statement, so NGA004
    # must not also fire on the same line.
    assert _codes(findings) == ["NGA003"]


def test_check_paths_walks_directory_and_skips_syntax_errors(tmp_path):
    good = tmp_path / "a.py"
    good.write_text("import time\n\nasync def f():\n    time.sleep(1)\n")
    broken = tmp_path / "b.py"
    broken.write_text("def broken(:\n")  # invalid syntax, must not crash the run
    findings = list(check_paths([tmp_path]))
    assert len(findings) == 1
    assert findings[0].code == "NGA001"


def test_lambda_inside_async_def_is_not_treated_as_async_context():
    # a lambda body can't yield regardless of its enclosing scope; open() inside
    # one shouldn't be flagged as an async-context blocking call.
    source = "async def f():\n    g = lambda: open('x')\n"
    findings = check_source(source)
    assert findings == []


# ---------------------------------------------------------------------------
# Regression tests for the issues found in adversarial (Codex) review.
# ---------------------------------------------------------------------------

def test_nga004_no_false_positive_on_sync_method_sharing_async_name():
    # Codex #1: a sync method must not be flagged just because another class has
    # an async method of the same name. Ambiguous names are skipped.
    source = (
        "class A:\n    async def refresh(self): ...\n\n"
        "class B:\n    def refresh(self): ...\n"
        "    def f(self):\n        self.refresh()\n"
    )
    assert check_source(source) == []


def test_nga001_from_import_sleep_resolved():
    # Codex #2 (false negative): `from time import sleep; sleep(1)`
    source = "from time import sleep\n\nasync def f():\n    sleep(1)\n"
    assert _codes(check_source(source)) == ["NGA001"]


def test_nga002_aliased_module_import_resolved():
    # Codex #2 (false negative): `import requests as r; r.get(...)`
    source = "import requests as r\n\nasync def f():\n    r.get('http://x')\n"
    assert _codes(check_source(source)) == ["NGA002"]


def test_nga002_from_import_name_resolved():
    source = "from requests import get\n\nasync def f():\n    get('http://x')\n"
    assert _codes(check_source(source)) == ["NGA002"]


def test_param_shadowing_module_is_not_flagged():
    # Codex #2 (false positive): a parameter named `time` shadows the module.
    assert check_source("async def f(time):\n    time.sleep(1)\n") == []
    assert check_source("async def f(requests):\n    requests.get('x')\n") == []


def test_local_create_task_function_not_flagged_as_nga003():
    # Codex #4: a local create_task() that isn't asyncio.create_task
    source = "def create_task(x): ...\n\ndef f():\n    create_task(1)\n"
    assert check_source(source) == []


def test_nga004_returned_coroutine_flagged():
    # Codex #6: `return helper()` where helper is async = forgotten await
    source = "async def helper(): ...\n\nasync def f():\n    return helper()\n"
    assert _codes(check_source(source)) == ["NGA004"]


def test_nga004_sync_def_returning_coroutine_not_flagged():
    # pass-through wrapper (redis lock.release, jinja2 Macro.__call__): the caller awaits
    source = "async def helper(): ...\n\ndef f():\n    return helper()\n"
    assert check_source(source) == []


def test_nga004_async_generator_call_not_flagged():
    # calling an async generator returns an async iterator; awaiting it would be a TypeError
    source = (
        "async def gen():\n    yield 1\n\n"
        "def f():\n    return gen()\n\n"
        "async def g():\n    gen()\n    return gen()\n"
    )
    assert check_source(source) == []


def test_nga004_yield_in_nested_def_does_not_make_outer_a_generator():
    source = (
        "async def helper():\n    def inner():\n        yield 1\n    return 1\n\n"
        "async def f():\n    helper()\n"
    )
    assert _codes(check_source(source)) == ["NGA004"]


def test_nga003_returned_task_not_flagged():
    # returning the Task hands its strong reference to the caller
    source = (
        "import asyncio\n\nasync def helper(): ...\n\n"
        "def f():\n    return asyncio.ensure_future(helper())\n\n"
        "async def g():\n    return asyncio.create_task(helper())\n"
    )
    assert check_source(source) == []


def test_open_not_in_default_blocking_set():
    # Codex #3: builtin open() is too noisy to fail CI on by default
    assert check_source("async def f():\n    open('cfg.json')\n") == []


def test_decorator_expression_checked_in_enclosing_scope():
    # Codex #7: a decorator runs at import time, not inside the coroutine
    source = (
        "import requests\n\ndef deco(x): return x\n\n"
        "@deco(requests.get('http://x'))\n"
        "async def f():\n    pass\n"
    )
    assert check_source(source) == []


def test_columns_are_one_based():
    # Codex #11: 1-based columns like other linters
    findings = check_source("import time\n\nasync def f():\n    time.sleep(1)\n")
    assert len(findings) == 1
    assert findings[0].col == 5  # 4 spaces of indent -> column 5


def test_pyi_stub_skipped_in_directory(tmp_path):
    # Codex #10: .pyi stubs contain no runtime code
    (tmp_path / "s.pyi").write_text("import time\nasync def f():\n    time.sleep(1)\n")
    assert list(check_paths([tmp_path])) == []


# ---------------------------------------------------------------------------
# Directory-walk exclusions: linting a project root must not lint its virtualenv.
# ---------------------------------------------------------------------------

BLOCKING = "import time\n\nasync def f():\n    time.sleep(1)\n"


def _project(root: Path, *vendor_dirs: str) -> None:
    """A clean app.py plus one blocking-call module inside each vendor dir."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("async def index():\n    pass\n")
    for rel in vendor_dirs:
        d = root / rel
        d.mkdir(parents=True)
        (d / "mod.py").write_text(BLOCKING)


def test_venv_inside_a_project_root_is_not_linted(tmp_path):
    # The bug: `nicegui-asyncio-lint .` on a brand-new project reported 15 findings,
    # all of them from .venv/lib/.../site-packages — third-party code the user cannot
    # fix — and exited 1, so the documented "CI-friendly exit code" was always red.
    _project(tmp_path, ".venv/lib/python3.12/site-packages/anyio")
    assert list(check_paths([tmp_path])) == []


def test_each_default_excluded_dir_name_is_pruned(tmp_path):
    for name in DEFAULT_EXCLUDE_DIRS:
        root = tmp_path / name.replace("-", "_")
        _project(root, name)
        assert list(check_paths([root])) == [], name


def test_dot_directories_are_pruned(tmp_path):
    _project(tmp_path, ".tox/py312/lib/pkg", ".git/hooks", ".mypy_cache/3.12")
    assert list(check_paths([tmp_path])) == []


def test_an_explicitly_named_root_is_never_pruned(tmp_path):
    # Pointing the tool AT a venv still scans it — exclusion prunes discovered
    # directories, not the paths the user asked for.
    _project(tmp_path, ".venv/pkg")
    assert [f.code for f in check_paths([tmp_path / ".venv"])] == ["NGA001"]
    # ...but a name in the exclude set still prunes below the root, so a whole
    # venv needs its site-packages named directly (or exclude_dirs=()).
    _project(tmp_path / "b", ".venv/lib/site-packages/pkg")
    assert list(check_paths([tmp_path / "b" / ".venv"])) == []
    assert [f.code for f in check_paths([tmp_path / "b/.venv/lib/site-packages"])] == ["NGA001"]


def test_exclusions_can_be_turned_off(tmp_path):
    _project(tmp_path, ".venv/pkg", "node_modules/x")
    findings = list(check_paths([tmp_path], exclude_dirs=(), skip_dot_dirs=False))
    assert [f.code for f in findings] == ["NGA001", "NGA001"]


def test_extra_exclude_dir_names_are_honoured(tmp_path):
    _project(tmp_path, "vendor")
    assert [f.code for f in check_paths([tmp_path])] == ["NGA001"]
    assert list(check_paths([tmp_path], exclude_dirs={"vendor"})) == []


def test_cli_exits_zero_on_a_clean_project_with_a_venv(tmp_path, capsys):
    _project(tmp_path, ".venv/lib/site-packages/pkg")
    assert main([str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_cli_no_default_excludes_flag_scans_the_venv(tmp_path):
    _project(tmp_path, ".venv/lib/site-packages/pkg")
    assert main(["--no-default-excludes", str(tmp_path)]) == 1


def test_cli_exclude_flag_adds_names(tmp_path):
    _project(tmp_path, "vendor", "generated")
    assert main([str(tmp_path)]) == 1
    assert main(["--exclude", "vendor,generated", str(tmp_path)]) == 0
    # a comma-SPACE list is what people actually type
    assert main(["--exclude", "vendor, generated", str(tmp_path)]) == 0


def test_an_explicitly_named_pyi_stub_is_still_skipped(tmp_path):
    # Codex review of this change: the .pyi skip used to cover the explicit-file
    # branch too, and moving the filter into the directory walk dropped it.
    stub = tmp_path / "s.pyi"
    stub.write_text(BLOCKING)
    assert list(check_paths([stub])) == []


def test_findings_are_globally_path_sorted(tmp_path):
    # Codex review: os.walk is top-down, so root files come before subdirectories;
    # the old sorted(rglob(...)) ordered every path together. Keep that.
    (tmp_path / "a").mkdir()
    (tmp_path / "z.py").write_text(BLOCKING)
    (tmp_path / "a" / "x.py").write_text(BLOCKING)
    assert [Path(f.path).name for f in check_paths([tmp_path])] == ["x.py", "z.py"]
