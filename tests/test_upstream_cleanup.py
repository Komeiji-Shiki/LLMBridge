"""上游收尾 helper：清理失败不能盖过主流程，但任务自身的异常要留下记录。"""
import asyncio
import logging

from routes._direct_api_utils import cancel_and_wait, close_quietly


def test_close_quietly_releases_the_iterator():
    closed = []

    async def stream():
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    async def run():
        iterator = stream()
        await anext(iterator)
        await close_quietly(iterator)

    asyncio.run(run())
    assert closed == [True]


def test_close_quietly_does_not_raise_but_keeps_a_trace(caplog):
    class Broken:
        async def aclose(self):
            raise OSError('连接已断开')

    with caplog.at_level(logging.DEBUG, logger='routes._direct_api_utils'):
        asyncio.run(close_quietly(Broken()))
    assert any('连接已断开' in record.getMessage() for record in caplog.records)


def test_cancel_and_wait_cancels_a_pending_task(caplog):
    async def run():
        task = asyncio.ensure_future(asyncio.sleep(30))
        await asyncio.sleep(0)
        await cancel_and_wait(task)
        return task

    with caplog.at_level(logging.WARNING):
        task = asyncio.run(run())
    assert task.cancelled()
    assert not caplog.records


def test_cancel_and_wait_reports_the_tasks_own_failure(caplog):
    async def fail():
        raise RuntimeError('上游炸了')

    async def run():
        task = asyncio.ensure_future(fail())
        await asyncio.sleep(0)
        await cancel_and_wait(task)

    with caplog.at_level(logging.WARNING, logger='routes._direct_api_utils'):
        asyncio.run(run())
    assert any('上游炸了' in record.getMessage() for record in caplog.records)
