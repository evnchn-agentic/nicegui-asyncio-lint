"""Fixture: the fixed/idiomatic version of every case in bad_examples.py.
The checker must report ZERO findings against this file.
"""
import asyncio

import httpx


async def handler_ok_sleep():
    await asyncio.sleep(2)


async def handler_ok_requests():
    async with httpx.AsyncClient() as client:
        data = await client.get("https://example.com")
    return data


async def handler_ok_subprocess():
    proc = await asyncio.create_subprocess_exec("echo", "hi")
    await proc.wait()


async def helper_task():
    await asyncio.sleep(1)


async def handler_ok_task_kept():
    # kept a strong reference — the documented-correct pattern
    task = asyncio.create_task(helper_task())
    await task


async def target_coro():
    await asyncio.sleep(0.1)


async def handler_ok_awaited():
    await target_coro()


class Widget:
    async def refresh(self) -> None:
        await asyncio.sleep(0.05)

    async def on_click(self) -> None:
        await self.refresh()


def sync_helper_using_open():
    # NOT inside an async def — open() here is fine, must not be flagged
    with open(__file__) as f:
        return f.read(1)
