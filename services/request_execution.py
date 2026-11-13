"""Shared upstream retries. Prelude events are held until a business event.

The existing model-level retry and long sticky cooldown settings are retained.
After any business event is released this executor never replays the request.
"""
import asyncio
import copy
from functools import wraps
import inspect
import json
import logging
import time
from core.request_context import current_request, credential_identity, endpoint_identity
from services.gemini_signature_recovery import GeminiTextSignatureRecovery
from services.gemini_system_prompt import strip_claude_agent_sdk_prefix
from services.model_replacements import apply_known_replacement, learn_replacement
from services.protocol_events import payloads, error_status, is_business_event, is_terminal, upstream_response_model
from services.provider_capabilities import apply_native_tool_defaults, protocol_name
from core.constants import RetryDefaults

logger = logging.getLogger(__name__)


def _error_chunk(message, byte_mode):
    value = {'error': {'message': message, 'type': 'upstream_stream_error', 'code': 502}}
    return ('data: ' + json.dumps(value) + '\n\n').encode() if byte_mode else value


def _credential_error_chunk(values, status, fallback, byte_mode, stream):
    """上游拒绝网关凭据按 502 返回；原样给 401 会让客户端以为自己的 Key 失效而不再重试。"""
    error = next((value.get('error') for value in values if isinstance(value, dict) and value.get('error')), None)
    message = (error.get('message') if isinstance(error, dict) else error) or fallback
    value = {'error': {'message': f'上游鉴权失败（HTTP {status}），与客户端 Key 无关：{message}',
                       'type': 'api_error', 'code': 502, 'upstream_status': status}}
    if not byte_mode:
        return value
    data = json.dumps(value, ensure_ascii=False).encode()
    return b'data: ' + data + b'\n\n' if stream else data


def _is_gemini_signature_rejection(value):
    if not isinstance(value, dict) or value.get('_http_status') != 400:
        return False
    error = value.get('error')
    if not isinstance(error, dict):
        return False
    # Vertex 和 Gemini API 对无效思考签名返回不同的错误文案。
    message = str(error.get('message', '')).lower()
    return 'invalid thought signature' in message or 'corrupted thought signature' in message


def retry_before_output(method=None, *, output_bytes=False):
    if method is None:
        return lambda wrapped: retry_before_output(wrapped, output_bytes=output_bytes)
    signature = inspect.signature(method)

    @wraps(method)
    async def execute(*args, **kwargs):
        context = current_request.get()
        if context is None or context.transport_depth:
            async for chunk in method(*args, **kwargs):
                yield chunk
            return
        from routes._direct_api_utils import (normalize_auto_retry_config, get_api_key, mark_sticky_key_cooldown, is_quota_exceeded,
                                              _get_valid_keys, is_retryable_other_status, UPSTREAM_CREDENTIAL_STATUSES)
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        body = bound.arguments.get('request_body') or {}
        if 'request_body' in bound.arguments:
            body = apply_native_tool_defaults(body, context.endpoint)
            strip_claude_agent_sdk_prefix(body, context.endpoint, context.model)
            bound.arguments['request_body'] = body
        stream = bound.arguments.get('stream_override')
        if stream is None:
            stream = bound.arguments.get('stream', body.get('stream', False))
        config = context.endpoint
        retry = normalize_auto_retry_config(config)
        strategy = config.get('api_key_strategy', 'round_robin')
        keys = config.get('api_keys') or config.get('api_key')
        cooldown = int(config.get('api_key_cooldown_seconds') or RetryDefaults.API_KEY_COOLDOWN_SECONDS)
        attempts = 1 + (retry.get('max_retries', 0) if retry.get('enabled') else 0)
        if strategy == 'sticky':
            attempts = max(attempts, len(_get_valid_keys(keys)) + 1)
        # Server-side stored response references are bound to their original account.
        pinned = bool(context.request_body.get('previous_response_id') or context.request_body.get('previous_interaction_id'))
        signature_recovery = None
        if protocol_name(config) == 'gemini':
            scope = endpoint_identity({**config, 'model_id': config.get('model_id') or context.model})
            signature_recovery = GeminiTextSignatureRecovery(scope)
        context.gemini_signature_recovery = signature_recovery
        select_key = strategy == 'sticky'
        model_switched = False
        retries_remaining = attempts - 1
        context.transport_depth += 1
        try:
            while True:
                if select_key and not pinned and 'api_key' in bound.arguments:
                    bound.arguments['api_key'] = await get_api_key(context.model, keys, strategy=strategy, cooldown_seconds=cooldown)
                key = bound.arguments.get('api_key') or ''
                context.credential_fingerprint = credential_identity(key)
                if 'request_body' in bound.arguments:
                    if context.responses_history is not None:
                        prepared = copy.deepcopy(body)
                        prepared['input'] = await context.responses_history.restore_input()
                        bound.arguments['request_body'] = prepared
                    elif signature_recovery is not None:
                        bound.arguments['request_body'] = signature_recovery.prepare(body)
                    context.upstream_request = copy.deepcopy(bound.arguments['request_body'])
                apply_known_replacement(bound.arguments, config)
                info = {'attempt': len(context.attempts) + 1,
                        'started_ms': round((time.perf_counter() - context.started) * 1000, 2)}
                context.attempts.append(info)
                # 每次重试只记录本次响应里的型号，避免沿用已放弃的上游尝试。
                context.outcome.pop('upstream_model', None)
                context.mark('upstream_start')
                prefix, total, committed, terminal = [], 0, False, False
                values = []
                # 空响应没有首块可用于判断类型，沿用入口声明的输出格式。
                failed, error_text, byte_mode = None, '', output_bytes
                iterator = method(*bound.args, **bound.kwargs)
                try:
                    async for chunk in iterator:
                        context.mark('first_byte')
                        byte_mode = isinstance(chunk, (bytes, str))
                        values = payloads(chunk)
                        failure = next((error_status(value) for value in values if error_status(value) is not None), None)
                        if failure is not None:
                            failed, error_text = failure, str(values)
                            prefix.append(chunk)
                            if committed:
                                yield chunk
                            break
                        terminal = terminal or any(is_terminal(value) for value in values)
                        if not stream:
                            prefix.append(chunk)
                            continue
                        if response_model := upstream_response_model(values):
                            context.outcome['upstream_model'] = response_model
                        if not committed:
                            prefix.append(chunk)
                            total += len(chunk) if isinstance(chunk, (bytes, str)) else len(json.dumps(chunk))
                            business = any(is_business_event(value) for value in values)
                            if total > 1024 * 1024 and not business:
                                failed, error_text = 502, '上游未产生业务事件且前导事件超过缓冲上限'
                                prefix = [_error_chunk(error_text, byte_mode)]
                                break
                            if business or terminal:
                                committed = True
                                if business and signature_recovery is not None:
                                    signature_recovery.confirm()
                                context.mark('first_business')
                                for saved in prefix:
                                    yield saved
                                prefix.clear()
                        else:
                            yield chunk
                    if not stream and failed is None:
                        raw = b''.join(chunk.encode() if isinstance(chunk, str) else chunk for chunk in prefix) if byte_mode else None
                        values = payloads(raw) if raw is not None else prefix
                        failed = next((error_status(value) for value in values if error_status(value) is not None), None)
                        error_text = str(values) if failed else ''
                        if not failed and (response_model := upstream_response_model(values)):
                            context.outcome['upstream_model'] = response_model
                        if not failed and signature_recovery is not None and any(is_business_event(value) for value in values):
                            signature_recovery.confirm()
                    if stream and failed is None and not terminal:
                        failed = 502
                        error_text = '上游流在终态事件之前中断'
                        error = _error_chunk(error_text, byte_mode)
                        if committed:
                            yield error
                        else:
                            prefix = [error]
                except asyncio.CancelledError:
                    info['error'] = 'client_disconnected'
                    raise
                finally:
                    await iterator.aclose()
                    info['duration_ms'] = round((time.perf_counter() - context.started) * 1000 - info['started_ms'], 2)
                info['status'] = failed or 200
                if signature_recovery is not None and signature_recovery.omitted:
                    info['omitted_thought_signatures'] = len(signature_recovery.omitted)
                if (signature_recovery is not None and failed == 400 and not committed
                        and any(_is_gemini_signature_rejection(value) for value in values)):
                    removed = signature_recovery.plan_retry()
                    if removed:
                        # 签名校验失败发生在生成前；沿用本次凭据，只重试一次。
                        select_key = False
                        logger.info('[RETRY] Gemini 拒绝思考签名，移除 %d 个文本签名后重试', removed)
                        continue
                if failed and not committed and not model_switched and learn_replacement(values, bound.arguments, config):
                    # 弃用错误发生在生成前，换型号不算一次失败的重试；同一请求只切一次。
                    model_switched = True
                    select_key = False
                    continue
                quota = bool(failed and strategy == 'sticky' and is_quota_exceeded(failed, error_text))
                if quota:
                    await mark_sticky_key_cooldown(context.model, keys, key, cooldown)
                retryable = quota or (retry.get('enabled') and (
                    retry.get('retry_on_' + str(failed), False)
                    or (failed is not None and is_retryable_other_status(failed) and retry.get('retry_on_other_errors'))))
                if pinned and quota:
                    retryable = False
                if failed and not committed and retryable and retries_remaining:
                    await asyncio.sleep(float(retry.get('retry_delay_seconds') or 0))
                    retries_remaining -= 1
                    select_key = True
                    continue
                if failed in UPSTREAM_CREDENTIAL_STATUSES and not committed:
                    prefix = [_credential_error_chunk(values, failed, error_text, byte_mode, stream)]
                if not committed:
                    if not failed:
                        context.mark('first_business')
                    for chunk in prefix:
                        yield chunk
                return
        finally:
            context.gemini_signature_recovery = None
            context.transport_depth -= 1
    return execute
