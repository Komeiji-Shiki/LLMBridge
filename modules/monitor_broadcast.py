"""每个监控连接一个有界发送队列，避免慢连接积压并发发送任务。"""

import asyncio
from utils.task_registry import spawn


class MonitorBroadcaster:
    def __init__(self, clients, timeout=2.0, queue_size=64):
        self.clients = clients
        self.timeout = timeout
        self.queue_size = queue_size
        self._workers = {}

    def publish(self, data):
        for client in tuple(self.clients):
            state = self._workers.get(client)
            if state is None:
                queue = asyncio.Queue(maxsize=self.queue_size)
                worker = spawn(self._send(client, queue), name='monitor-client-writer')
                state = self._workers[client] = (queue, worker)
            queue, worker = state
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # 超载时让客户端重连并重新读取持久日志，不无限保留消息。
                self.remove(client)
                spawn(self._close(client, 1013), name='monitor-slow-client-close')

    async def _close(self, client, code):
        try:
            await asyncio.wait_for(client.close(code=code), timeout=self.timeout)
        except Exception:
            pass

    async def _send(self, client, queue):
        try:
            while True:
                data = await queue.get()
                await asyncio.wait_for(client.send_json(data), timeout=self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.clients.discard(client)
            await self._close(client, 1011)
        finally:
            state = self._workers.get(client)
            if state and state[1] is asyncio.current_task():
                self._workers.pop(client, None)

    def remove(self, client):
        self.clients.discard(client)
        state = self._workers.pop(client, None)
        if state:
            state[1].cancel()
