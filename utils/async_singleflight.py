"""相同查询共享一个进行中的任务，取消等待不取消其他调用者的查询。"""

import asyncio


class AsyncSingleFlight:
    def __init__(self):
        self.generation = 0
        self._tasks = {}

    def invalidate(self):
        self.generation += 1

    async def run(self, key, factory):
        task_key = (self.generation, key)
        task = self._tasks.get(task_key)
        if task is None:
            task = asyncio.create_task(factory())
            self._tasks[task_key] = task

            def finish(completed):
                self._tasks.pop(task_key, None)
                if not completed.cancelled():
                    completed.exception()

            task.add_done_callback(finish)
        return await asyncio.shield(task)
