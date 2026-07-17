from pathlib import Path

from nicegui_asyncio_lint.checker import check_source, check_paths

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
