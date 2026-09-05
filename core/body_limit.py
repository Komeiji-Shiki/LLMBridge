"""Bound request bytes while receiving, including chunked HTTP uploads."""
from fastapi import HTTPException


class RequestBodyLimitMiddleware:
    def __init__(self, app, max_bytes):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        received = 0
        async def checked():
            nonlocal received
            message = await receive()
            received += len(message.get('body', b''))
            if received > self.max_bytes:
                raise HTTPException(413, '上传内容超过大小上限')
            return message
        await self.app(scope, checked, send)
