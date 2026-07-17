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
