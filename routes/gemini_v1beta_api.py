"""Gemini entry point; native transport, retries and accounting are shared."""
import copy
from datetime import datetime
from urllib.parse import unquote
from fastapi import HTTPException
from core.config_loader import MODEL_ROUND_ROBIN_INDEX, MODEL_ROUND_ROBIN_LOCK
from core.model_archive import is_model_archived
from services.native_exchange import forward_native_exchange


async def select_gemini_endpoint(model_name, model_map):
    config = model_map.get(model_name)
    if not config or is_model_archived(config):
        raise HTTPException(404, f"模型 '{model_name}' 未在配置中找到")
    if isinstance(config, list):
        async with MODEL_ROUND_ROBIN_LOCK:
            index = MODEL_ROUND_ROBIN_INDEX.get(model_name, 0) % len(config)
            MODEL_ROUND_ROBIN_INDEX[model_name] = (index + 1) % len(config)
        config = config[index]
    if config.get('api_type') != 'gemini_native':
        raise HTTPException(400, f"模型 '{model_name}' 不是 Gemini 原生 API 类型")
    return copy.deepcopy(config)


async def gemini_native_api(model_name, request, MODEL_ENDPOINT_MAP, monitoring_service,
                            direct_api_service, last_activity_time_setter, aiohttp_session=None):
    last_activity_time_setter(datetime.now())
    model_name = unquote(model_name)
    try:
        body = await request.json()
    except (ValueError, UnicodeError) as error:
        raise HTTPException(400, '无效的 JSON 请求体') from error
    if not isinstance(body, dict):
        raise HTTPException(400, '请求体必须是 JSON 对象')
    config = await select_gemini_endpoint(model_name, MODEL_ENDPOINT_MAP)
    stream = request.url.path.endswith(':streamGenerateContent') or request.query_params.get('alt') == 'sse'
    return await forward_native_exchange(body, config, model_name, direct_api_service,
                                         monitoring_service, stream=stream, gemini_response=True)
