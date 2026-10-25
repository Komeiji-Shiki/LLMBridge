"""用真实本地 HTTP 上传验证大文件与短引用，不向外部模型发送请求。"""
import asyncio
import base64
import io
import json
from pathlib import Path
import shutil
import socket
from unittest.mock import AsyncMock, MagicMock
import wave

import pytest
from fastapi import FastAPI
from hypercorn.asyncio import serve
from hypercorn.config import Config

from converters.anthropic_openai import convert_anthropic_to_openai_request
from core.audio_files import AudioFiles
from core.request_context import RequestContext, current_request
from routes import api_routes
from routes.audio_files import router
from services.audio_input import resolve_audio_references
from services.direct_api_service import DirectAPIService


def test_uploaded_audio_over_four_mib_uses_short_history_and_both_protocols(tmp_path, monkeypatch):
    node = shutil.which("node")
    if not node:
        pytest.skip("需要 Node 验证 Claude Code 的本地传输模块")
    monkeypatch.setitem(api_routes.CONFIG, "api_key", "test-audio-key")
    monkeypatch.setitem(api_routes.MODEL_ENDPOINT_MAP, "audio-test", {"api_type": "gemini_native"})
    helper = Path(__file__).resolve().parents[1] / "docs/examples/claude_code_audio_input.cjs"
    audio_path = tmp_path / "大文件.wav"
    with wave.open(str(audio_path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b"\0\0" * (2 * 1024 * 1024 + 128))
    assert audio_path.stat().st_size > 4 * 1024 * 1024

    async def run():
        application = FastAPI()
        application.include_router(router)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        config = Config()
        config.bind = [f"127.0.0.1:{port}"]
        config.accesslog = config.errorlog = None
        stopping = asyncio.Event()
        server = asyncio.create_task(serve(application, config, shutdown_trigger=stopping.wait))
        try:
            for _ in range(100):
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.02)
            code = """
const {createAudioInput} = require(process.argv[1]);
const env = {ANTHROPIC_BASE_URL:process.argv[3], ANTHROPIC_API_KEY:'test-audio-key', ANTHROPIC_MODEL:'audio-test'};
(async () => {
  let blocked = false;
  try {
    await createAudioInput({env, fetch:async()=>({ok:false})}).read({file_path:process.argv[2]+'.missing',mime_type:'audio/wav'});
  } catch(error) { blocked = error.message.includes('更新并重启'); }
  if(!blocked) throw new Error('旧网关没有在读取文件前停止');
  const result = await createAudioInput({env}).read({file_path:process.argv[2],mime_type:'audio/wav'});
  process.stdout.write(JSON.stringify(result));
})().catch(error=>{process.stderr.write(error.stack);process.exitCode=1;});
"""
            process = await asyncio.create_subprocess_exec(
                node, "-e", code, str(helper), str(audio_path), f"http://127.0.0.1:{port}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await process.communicate()
            assert process.returncode == 0, error.decode()
            descriptor = json.loads(output)
            assert descriptor["size"] == audio_path.stat().st_size
            assert len(descriptor["reference"]) < 100
            store = AudioFiles()
            assert store.paths(descriptor["file_id"])[0].read_bytes() == audio_path.read_bytes()
            with pytest.raises(Exception) as denied:
                store.metadata(descriptor["file_id"], "another-owner")
            assert denied.value.status_code == 404
            request = {"model": "audio-test", "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "read_audio", "name": "Read", "input": {"file_path": str(audio_path)}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read_audio", "content": descriptor["reference"]}]},
            ]}
            messages = convert_anthropic_to_openai_request(request)["messages"]
            assert len(json.dumps(messages)) < 1000
            token = current_request.set(RequestContext(owner_id="admin", authenticated=True))
            try:
                resolved = await resolve_audio_references(messages, None, "", None, {})
            finally:
                current_request.reset(token)
            expected = base64.b64encode(audio_path.read_bytes()).decode("ascii")
            for protocol in ("generate_content", "interactions"):
                session = MagicMock()
                response = MagicMock(status=200)
                response.json = AsyncMock(return_value={"candidates": [], "id": "test", "status": "completed", "steps": []})
                session.post.return_value.__aenter__ = AsyncMock(return_value=response)
                session.post.return_value.__aexit__ = AsyncMock(return_value=False)
                service = DirectAPIService(session)
                method = service.call_gemini_native_api if protocol == "generate_content" else service.call_gemini_interactions_api
                results = [item async for item in method("test-key", "demo", resolved, stream=False)]
                assert not any("error" in item for item in results)
                body = json.loads(session.post.call_args.kwargs["data"])
                if protocol == "generate_content":
                    assert body["contents"][-1]["parts"][-1] == {"inlineData": {"mimeType": "audio/wav", "data": expected}}
                else:
                    assert body["input"][-1]["content"][-1] == {"type": "audio", "mime_type": "audio/wav", "data": expected}
        finally:
            stopping.set()
            await server
    asyncio.run(run())
