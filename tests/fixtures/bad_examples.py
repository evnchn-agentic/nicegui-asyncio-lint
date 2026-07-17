"""Fixture: one instance of each of the four footguns this checker flags.
Intentionally NOT meant to run cleanly — it is scanned by the checker in tests.
"""
import asyncio
import os
import subprocess
import time

import requests


async def handler_blocking_sleep():
    # NGA001
    time.sleep(2)


async def handler_blocking_requests():
    # NGA002
    data = requests.get("https://example.com")
    return data


async def handler_blocking_subprocess():
    # NGA002
    subprocess.run(["echo", "hi"])


async def handler_os_system():
    # NGA002
    os.system("echo hi")


async def helper_task():
    await asyncio.sleep(1)


async def handler_fire_and_forget():
    # NGA003 — return value of create_task discarded
    asyncio.create_task(helper_task())


async def target_coro():
    await asyncio.sleep(0.1)


async def handler_missing_await():
    # NGA004 — coroutine created, never awaited, silently dropped
    target_coro()


class Widget:
    async def refresh(self) -> None:
        await asyncio.sleep(0.05)

    async def on_click(self) -> None:
        # NGA004 — self.refresh() is async, called without await
        self.refresh()
