from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import os

from oh_my_llm import AbortSignal


async def _yield_for_cancellation(signal: AbortSignal) -> None:
    if signal.aborted:
        raise asyncio.CancelledError
    await asyncio.sleep(0)
    if signal.aborted:
        raise asyncio.CancelledError


async def _acquire_uninterruptibly(lock: asyncio.Lock) -> None:
    waiter = asyncio.ensure_future(lock.acquire())
    try:
        await waiter
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        await waiter
        raise


class _FileMutationQueue:
    def __init__(self) -> None:
        self._meta = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def key_for(self, resolved: str) -> str:
        try:
            if os.path.exists(resolved):
                return os.path.realpath(resolved)
        except OSError:
            pass
        return os.path.abspath(resolved)

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._meta:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            self._refs[key] = self._refs.get(key, 0) + 1
            return lock

    async def _drop(self, key: str) -> None:
        async with self._meta:
            remaining = self._refs[key] - 1
            if remaining == 0:
                del self._refs[key]
                del self._locks[key]
            else:
                self._refs[key] = remaining

    @asynccontextmanager
    async def hold(self, key: str, signal: AbortSignal) -> AsyncIterator[None]:
        lock = await self._lock_for(key)
        acquired = False
        try:
            try:
                await _acquire_uninterruptibly(lock)
            except asyncio.CancelledError:
                acquired = True
                raise
            acquired = True
            await after_queue_hold(signal, key)
            yield
        finally:
            if acquired:
                lock.release()
            await self._drop(key)


MUTATION_QUEUE = _FileMutationQueue()


async def after_queue_hold(signal: AbortSignal, key: str) -> None:
    del key
    await _yield_for_cancellation(signal)
