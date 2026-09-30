# Responses WebSocket

`/v1/responses` 同时提供原有 HTTP POST 和新的 WebSocket 会话。模型端点需要配置为
`responses_native`，上游本身也需要支持 Responses WebSocket。

客户端使用现有 `Authorization: Bearer ...` 或 `x-api-key` 认证。握手仅确认身份，
每个 `response.create`、`response.steer` 分别检查模型权限和 RPM，避免握手重复消耗额度。
首个事件必须包含 `model`。Bridge 据此选择一次模型端点和密钥，后续事件使用同一上游连接；
切换模型请建立新连接。未指定模型的后续创建事件继续使用连接绑定的模型。

请求保留异步函数、自定义工具、结构化工具结果、推理密文和未知事件字段，只替换模型别名。
`response.steer` 及 accepted/pending/failed 事件直接转发。Bridge 不执行工具，调用方负责
按 `call_id` 管理待完成任务并回传结果，也不能把 accepted 当成指令已经应用。

每个上游响应独立记录用量和费用，客户端正常关闭已完成的响应不会被记为失败。
连接断开时会关闭对应上游，并把未完成响应记为失败；不会自动重连或重放已发送的工具调用。
握手失败以 `error` 事件返回状态，满载时保留上游的 `retry_after` 提示。

```python
from openai import AsyncOpenAI

async with AsyncOpenAI(api_key="BRIDGE_KEY", base_url="http://127.0.0.1:5100/v1") as client:
    async with client.responses.connect(max_retries=0) as connection:
        await connection.response.create(model="配置中的模型别名", input="Hello")
        async for event in connection:
            if event.type == "response.completed":
                break
```

Python SDK 客户端需要支持 Responses WebSocket，例如 `openai[realtime]>=3.8.0,<4`。
Bridge 自身复用现有 FastAPI/aiohttp，不需要安装 OpenAI SDK 才能提供该接口。
