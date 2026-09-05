"""Administrator capability diagnostics, native Playground and read-only tools."""
import asyncio
import copy
import json
import re
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Request
from core.config_loader import CONFIG, MODEL_ENDPOINT_MAP
from core.model_archive import is_model_archived
from core.request_context import RequestContext, current_request
from core.app_state import get_app_state
from core.db_stats import stats_db
from core.usage_analysis import usage_by_caller, estimate_current_prices
from modules.monitoring import monitoring_service
from services.provider_capabilities import PROVIDERS, diagnose_model, protocol_name
from services.native_exchange import forward_native_exchange

router = APIRouter(prefix='/api/admin', tags=['gateway-workspace'])
_playground_runs = TTLCache(maxsize=256, ttl=1800)


def model_config(alias, endpoint=0):
    raw = MODEL_ENDPOINT_MAP.get(alias)
    if not raw or is_model_archived(raw):
        raise HTTPException(404, '模型不存在或已归档')
    configs = raw if isinstance(raw, list) else [raw]
    if isinstance(endpoint, bool) or not isinstance(endpoint, int) or not 0 <= endpoint < len(configs):
        raise HTTPException(400, '端点编号无效')
    return copy.deepcopy(configs[endpoint])


@router.get('/capabilities')
async def capabilities():
    items = []
    for alias, raw in MODEL_ENDPOINT_MAP.items():
        if is_model_archived(raw):
            continue
        for index, config in enumerate(raw if isinstance(raw, list) else [raw]):
            items.append({**diagnose_model(alias, config), 'endpoint': index})
    return {'providers': PROVIDERS, 'models': items}


@router.get('/usage_by_caller')
async def caller_usage(start_date: str = None, end_date: str = None):
    if not stats_db.db_path.exists():
        return {'items': [], 'read_only': True}
    try:
        return await asyncio.to_thread(usage_by_caller, stats_db.db_path, start_date, end_date)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get('/current_price_analysis')
async def current_price_analysis(start_date: str = None, end_date: str = None):
    if not stats_db.db_path.exists():
        return {'items': [], 'read_only': True, 'historical_amounts_unchanged': True}
    try:
        return await asyncio.to_thread(estimate_current_prices, stats_db.db_path, copy.deepcopy(MODEL_ENDPOINT_MAP), start_date, end_date)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.post('/playground/run')
async def playground_run(request: Request):
    from routes.admin_routes import _read_admin_json
    data = await _read_admin_json(request)
    alias, body = data.get('model'), data.get('request')
    if not isinstance(alias, str) or not isinstance(body, dict):
        raise HTTPException(400, 'model 和 request 对象为必填项')
    config = model_config(alias, data.get('endpoint', 0))
    if config.get('api_type') not in ('direct_api', 'responses_native', 'anthropic_native', 'gemini_native'):
        raise HTTPException(400, '该模型不支持原生 Playground')
    session = data.get('session_id') or ''
    if not isinstance(session, str) or (session and not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', session)):
        raise HTTPException(400, '会话 ID 格式无效')
    stream = data.get('stream', body.get('stream', False))
    if not isinstance(stream, bool):
        raise HTTPException(400, 'stream 必须为布尔值')
    # This route uses the same existing administrator middleware as /api/admin/config.
    # It never bypasses public API-key validation and cannot select an arbitrary URL.
    context = current_request.get() or RequestContext()
    context.owner_id, context.owner_name, context.is_admin, context.authenticated = 'admin', '管理员', True, True
    if session:
        context.session_id, context.explicit_session = session, True
    # Progress retains only shared timing containers, never the request body or Key.
    _playground_runs[context.request_id] = RequestContext(
        request_id=context.request_id, session_id=context.session_id, started=context.started,
        marks=context.marks, attempts=context.attempts, outcome=context.outcome,
        endpoint={key: config.get(key) for key in ('api_type', 'upstream_protocol')})
    service = get_app_state().server.direct_api_service
    if service is None:
        raise HTTPException(503, '上游服务尚未就绪')
    try:
        return await forward_native_exchange(body, config, alias, service, monitoring_service,
                                             stream=stream, context=context)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


@router.get('/playground/runs/{request_id}')
async def playground_result(request_id: str):
    context = _playground_runs.get(request_id)
    if context is None:
        raise HTTPException(404, '运行记录已过期，完整日志可在监控中查询')
    return {'timings': context.snapshot(), 'outcome': context.outcome, 'finished': 'finished' in context.marks,
            'session_id': context.session_id, 'protocol': protocol_name(context.endpoint)}


@router.get('/tokenizer_trust')
async def tokenizer_trust():
    return {'sources': list(CONFIG.get('tokenizer_trusted_sources') or [])}


@router.get('/exchanges/{request_id}')
async def download_exchange(request_id: str):
    from pathlib import Path
    from fastapi.responses import FileResponse
    if not re.fullmatch(r'[a-f0-9]{32}', request_id):
        raise HTTPException(400, '请求 ID 格式无效')
    def find():
        return next(Path('logs/exchanges').glob('*/*/' + request_id + '.json.gz'), None)
    path = await asyncio.to_thread(find)
    if path is None:
        raise HTTPException(404, '完整归档尚未生成或已被手动清理')
    return FileResponse(path, filename=request_id + '.json.gz', media_type='application/gzip')


@router.post('/tokenizer_trust')
async def update_tokenizer_trust(request: Request):
    from routes.admin_routes import _read_admin_json, _CONFIG_FILE_LOCK, CONFIG_FILE, read_text_file, write_text_file
    from utils.jsonc_edit import set_jsonc_value
    from core.config_loader import load_config
    data = await _read_admin_json(request)
    sources = data.get('sources')
    if not isinstance(sources, list) or not all(isinstance(value, str) and value.strip() and value != '*' for value in sources):
        raise HTTPException(400, 'sources 必须为明确来源的字符串列表，不支持通配符')
    sources = list(dict.fromkeys(sources))
    async with _CONFIG_FILE_LOCK:
        content = await read_text_file(CONFIG_FILE)
        await write_text_file(CONFIG_FILE, set_jsonc_value(content, 'tokenizer_trusted_sources', sources))
        await asyncio.to_thread(load_config)
    return {'sources': sources, 'message': '已保存；下次加载该来源时生效'}


TOOL_DEFINITIONS = [
    {'name': 'list_gateway_models', 'description': '列出网关模型、原生工具和文档诊断，不返回密钥。',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'diagnose_gateway_model', 'description': '检查指定模型端点配置与已核对的官方能力，不发起计费请求。',
     'inputSchema': {'type': 'object', 'properties': {'model': {'type': 'string'}, 'endpoint': {'type': 'integer', 'minimum': 0}}, 'required': ['model'], 'additionalProperties': False}},
    {'name': 'gateway_usage_by_caller', 'description': '按稳定调用方 ID 和币种读取历史用量。',
     'inputSchema': {'type': 'object', 'properties': {'start_date': {'type': 'string'}, 'end_date': {'type': 'string'}}, 'additionalProperties': False}},
    {'name': 'gateway_current_price_analysis', 'description': '用当前价格估算同一历史用量的成本，保留原历史金额。',
     'inputSchema': {'type': 'object', 'properties': {'start_date': {'type': 'string'}, 'end_date': {'type': 'string'}}, 'additionalProperties': False}},
]
for _definition in TOOL_DEFINITIONS:
    _definition['annotations'] = {'readOnlyHint': True, 'destructiveHint': False, 'openWorldHint': False}


@router.get('/tools')
async def list_tools():
    return {'tools': TOOL_DEFINITIONS}


async def execute_tool(name, arguments):
    definition = next((item for item in TOOL_DEFINITIONS if item['name'] == name), None)
    if definition is None:
        raise HTTPException(404, '未知网关工具')
    schema = definition['inputSchema']
    if not isinstance(arguments, dict) or set(arguments) - set(schema['properties']) or any(key not in arguments for key in schema.get('required', [])):
        raise HTTPException(400, '工具参数无效')
    if name == 'list_gateway_models':
        return await capabilities()
    if name == 'diagnose_gateway_model':
        if not isinstance(arguments['model'], str):
            raise HTTPException(400, 'model 必须为字符串')
        return diagnose_model(arguments['model'], model_config(arguments['model'], arguments.get('endpoint', 0)))
    for value in arguments.values():
        if not isinstance(value, str):
            raise HTTPException(400, '日期必须为 YYYY-MM-DD 字符串')
    if name == 'gateway_usage_by_caller':
        return await caller_usage(**arguments)
    return await current_price_analysis(**arguments)


@router.post('/tools/call')
async def call_tool(request: Request):
    from routes.admin_routes import _read_admin_json
    data = await _read_admin_json(request)
    return await execute_tool(data.get('name'), data.get('arguments', {}))
