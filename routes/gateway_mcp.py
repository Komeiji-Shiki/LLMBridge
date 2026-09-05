"""Stateless MCP Streamable HTTP for the four read-only gateway tools."""
from urllib.parse import urlsplit
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from routes.gateway_workspace import TOOL_DEFINITIONS, execute_tool

router = APIRouter(prefix='/api/admin/mcp', tags=['gateway-tools'])
VERSIONS = ('2025-06-18', '2025-03-26')


def validate_transport(request):
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        target = urlsplit(str(request.base_url))
        if parsed.scheme != target.scheme or parsed.netloc != target.netloc or parsed.path not in ('', '/'):
            raise HTTPException(403, 'Origin 不允许')
    version = request.headers.get('mcp-protocol-version', '2025-03-26')
    if version not in VERSIONS:
        raise HTTPException(400, '不支持的 MCP 协议版本')


@router.get('')
async def no_server_events(request: Request):
    validate_transport(request)
    return Response(status_code=405, headers={'Allow': 'POST'})


@router.post('')
async def mcp_request(request: Request):
    validate_transport(request)
    accept = request.headers.get('accept', '')
    if 'application/json' not in accept or 'text/event-stream' not in accept:
        raise HTTPException(406, 'Accept 需要同时包含 application/json 和 text/event-stream')
    try:
        message = await request.json()
    except (ValueError, UnicodeError):
        return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}, 400)
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}, 400)
    identifier = message.get('id')
    if 'id' not in message:
        return Response(status_code=202)
    if not isinstance(identifier, (str, int)) or isinstance(identifier, bool):
        return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request id'}}, 400)
    method, params = message.get('method'), message.get('params', {})
    if not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32602, 'message': 'Invalid params'}}
    if method == 'initialize':
        version = params.get('protocolVersion')
        result = {'protocolVersion': version if version in VERSIONS else VERSIONS[0],
                  'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'LLMBridge gateway tools', 'version': '1.0.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': TOOL_DEFINITIONS}
    elif method == 'tools/call':
        if params.get('name') not in [item['name'] for item in TOOL_DEFINITIONS]:
            return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32602, 'message': 'Unknown tool'}}
        import json
        try:
            value = await execute_tool(params.get('name'), params.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'structuredContent': value, 'isError': False}
        except HTTPException as error:
            result = {'content': [{'type': 'text', 'text': str(error.detail)}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': identifier, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': identifier, 'result': result}
