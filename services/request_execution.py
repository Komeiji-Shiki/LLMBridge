"""Shared upstream retries. Prelude events are held until a business event.

The existing model-level retry and long sticky cooldown settings are retained.
After any business event is released this executor never replays the request.
"""
import asyncio
import copy
from functools import wraps
import inspect
import json
import time
from core.request_context import current_request, credential_identity
from services.protocol_events import payloads, error_status, is_business_event, is_terminal


def _error_chunk(message, byte_mode):
    value = {'error': {'message': message, 'type': 'upstream_stream_error', 'code': 502}}
    return ('data: ' + json.dumps(value) + '\n\n').encode() if byte_mode else value


def retry_before_output(method):
    signature = inspect.signature(method)

    @wraps(method)
    async def execute(*args, **kwargs):
        context = current_request.get()
        if context is None or context.transport_depth:
            async for chunk in method(*args, **kwargs):
                yield chunk
            return
        from routes._direct_api_utils import normalize_auto_retry_config, get_api_key, mark_sticky_key_cooldown, is_quota_exceeded, _get_valid_keys
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        body = bound.arguments.get('request_body') or {}
        if 'request_body' in bound.arguments:
            from services.provider_capabilities import apply_native_tool_defaults
            body = apply_native_tool_defaults(body, context.endpoint)
            bound.arguments['request_body'] = body
        stream = bound.arguments.get('stream_override')
        if stream is None:
            stream = bound.arguments.get('stream', body.get('stream', False))
        config = context.endpoint
        retry = normalize_auto_retry_config(config)
        strategy = config.get('api_key_strategy', 'round_robin')
        keys = config.get('api_keys') or config.get('api_key')
        cooldown = int(config.get('api_key_cooldown_seconds') or 172800)
        attempts = 1 + (retry.get('max_retries', 0) if retry.get('enabled') else 0)
        if strategy == 'sticky':
            attempts = max(attempts, len(_get_valid_keys(keys)) + 1)
        # Server-side stored response references are bound to their original account.
        pinned = bool(context.request_body.get('previous_response_id'))
        context.transport_depth += 1
        try:
            for attempt in range(attempts):
                if (attempt or strategy == 'sticky') and not pinned and 'api_key' in bound.arguments:
                    bound.arguments['api_key'] = await get_api_key(context.model, keys, strategy=strategy, cooldown_seconds=cooldown)
                key = bound.arguments.get('api_key') or ''
                context.credential_fingerprint = credential_identity(key)
                if context.authenticated:
                    from core.conversation_store import conversation_store
                    try:
                        context.artifacts = await asyncio.to_thread(conversation_store.get, context.owner_id, context.cache_session(), 'signatures') or {}
                    except Exception:
                        context.artifacts = {}
                if 'request_body' in bound.arguments:
                    context.upstream_request = copy.deepcopy(bound.arguments['request_body'])
                info = {'attempt': attempt + 1, 'started_ms': round((time.perf_counter() - context.started) * 1000, 2)}
                context.attempts.append(info)
                context.mark('upstream_start')
                prefix, total, committed, terminal = [], 0, False, False
                failed, error_text, byte_mode = None, '', True
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
                        if not committed:
                            prefix.append(chunk)
                            total += len(chunk) if isinstance(chunk, (bytes, str)) else len(json.dumps(chunk))
                            if total > 1024 * 1024 and not any(is_business_event(value) for value in values):
                                failed, error_text = 502, '上游未产生业务事件且前导事件超过缓冲上限'
                                prefix = [_error_chunk(error_text, byte_mode)]
                                break
                            if any(is_business_event(value) for value in values) or terminal:
                                committed = True
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
                quota = bool(failed and strategy == 'sticky' and is_quota_exceeded(failed, error_text))
                if quota:
                    await mark_sticky_key_cooldown(context.model, keys, key, cooldown)
                retryable = quota or (retry.get('enabled') and (
                    retry.get('retry_on_' + str(failed), False)
                    or (failed is not None and failed >= 500 and retry.get('retry_on_other_errors'))))
                if pinned and quota:
                    retryable = False
                if failed and not committed and retryable and attempt + 1 < attempts:
                    await asyncio.sleep(float(retry.get('retry_delay_seconds') or 0))
                    continue
                if not committed:
                    if not failed:
                        context.mark('first_business')
                    for chunk in prefix:
                        yield chunk
                return
        finally:
            context.transport_depth -= 1
    return execute
