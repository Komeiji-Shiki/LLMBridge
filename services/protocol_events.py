"""Classify protocol events without changing their wire representation."""
import json


def payloads(chunk):
    if isinstance(chunk, dict):
        return [chunk]
    text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, bytes) else str(chunk)
    values = []
    if text.lstrip().startswith(('{', '[')):
        try:
            return [json.loads(text)]
        except ValueError:
            return []
    for block in text.replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip(' ') for line in block.splitlines() if line.startswith('data:'))
        if data.strip() == '[DONE]':
            values.append({'done': True})
        elif data:
            try:
                values.append(json.loads(data))
            except ValueError:
                pass
    return values


# Gemini 结束原因的唯一语义表：表内是正常结束，值为对应的 OpenAI finish_reason；
# 表外的值（含以后新增的）一律按异常终止上报，不能伪装成正常结束。
GEMINI_NORMAL_FINISH = {
    'STOP': 'stop',
    'MAX_TOKENS': 'length',
    # 能续传的中间跳不会走到这里；留到这里说明内容不完整，要让客户端知道被截断了。
    'CONTINUATION': 'length',
}
GEMINI_UNSPECIFIED_REASONS = ('FINISH_REASON_UNSPECIFIED', 'BLOCK_REASON_UNSPECIFIED')
GEMINI_FILTERED_FINISH = frozenset((
    'SAFETY', 'RECITATION', 'LANGUAGE', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII',
    'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT', 'IMAGE_RECITATION',
))


GEMINI_PROBABILITY_RANK = {'NEGLIGIBLE': 0, 'LOW': 1, 'MEDIUM': 2, 'HIGH': 3}


def gemini_finish_to_openai(reason):
    """异常终止返回 None：错误由 gemini_termination 单独上报。"""
    if reason in GEMINI_FILTERED_FINISH:
        return 'content_filter'
    return GEMINI_NORMAL_FINISH.get(reason)


def _gemini_verdict(value):
    """返回响应块里的（首个候选, promptFeedback），畸形字段按空处理。"""
    candidates = value.get('candidates')
    candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
    feedback = value.get('promptFeedback')
    return (candidate if isinstance(candidate, dict) else {}, feedback if isinstance(feedback, dict) else {})


def _rating_rank(rating):
    score = rating.get('probabilityScore')
    return (bool(rating.get('blocked')), GEMINI_PROBABILITY_RANK.get(rating.get('probability'), -1),
            score if isinstance(score, (int, float)) else 0)


def _merge_ratings(current, ratings):
    merged = dict(current or {})
    for rating in ratings if isinstance(ratings, list) else []:
        category = rating.get('category') if isinstance(rating, dict) else None
        if category and (category not in merged or _rating_rank(rating) >= _rating_rank(merged[category])):
            merged[category] = rating
    return merged


def merge_gemini_safety(current, value):
    """累积过滤依据：被拦的来源和原因，以及提示词、回复每个类别整条流里的最高评分。"""
    if not isinstance(value, dict):
        return current
    candidate, feedback = _gemini_verdict(value)
    result = dict(current or {})
    for field, holder in (('prompt_ratings', feedback), ('response_ratings', candidate)):
        ratings = _merge_ratings(result.get(field), holder.get('safetyRatings'))
        if ratings:
            result[field] = ratings
    reason, error = gemini_termination(value)
    if error:
        result['blocked'] = {'source': 'response' if candidate.get('finishReason') else 'prompt', 'reason': reason,
                             'message': candidate.get('finishMessage') or feedback.get('blockReasonMessage')}
    return result or current


def gemini_termination(value):
    """提取 Gemini 原始停止原因，将异常终止转换成可读错误。"""
    if not isinstance(value, dict):
        return None, None
    candidate, feedback = _gemini_verdict(value)
    reason = candidate.get('finishReason') or feedback.get('blockReason')
    if not reason or reason in GEMINI_NORMAL_FINISH or reason in GEMINI_UNSPECIFIED_REASONS:
        return reason, None
    detail = candidate.get('finishMessage') or feedback.get('blockReasonMessage')
    message = f'Gemini 上游终止生成：{reason}'
    ratings = candidate.get('safetyRatings') or feedback.get('safetyRatings')
    blocked = [f"{rating.get('category')}: {rating.get('probability')}" for rating in ratings
               if isinstance(rating, dict) and rating.get('blocked')] if isinstance(ratings, list) else []
    if blocked:
        message += f"（{'，'.join(blocked)}）"
    if detail:
        message += f'。上游说明：{detail}'
    return reason, {'code': 422, 'type': 'upstream_generation_error',
                    'reason': reason, 'message': message}


def error_status(value):
    if not isinstance(value, dict):
        return None
    error = value.get('error')
    kind = value.get('type') or value.get('event_type') or ''
    if kind == 'response.failed':
        error = (value.get('response') or {}).get('error') or {'code': 502}
    if error is None and kind != 'error':
        return None
    error = error if isinstance(error, dict) else {}
    code = value.get('_http_status') or error.get('code') or error.get('status')
    try:
        return int(code)
    except (TypeError, ValueError):
        kind = str(error.get('type') or value.get('code') or '')
        return 429 if 'rate_limit' in kind else 504 if 'timeout' in kind else 502


def is_terminal(value):
    if not isinstance(value, dict):
        return False
    if (value.get('promptFeedback') or {}).get('blockReason'):
        return True
    if value.get('done') or (value.get('type') or value.get('event_type')) in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.complete', 'interaction.completed'):
        return True
    if any(candidate.get('finishReason') for candidate in value.get('candidates', []) if isinstance(candidate, dict)):
        return True
    return any(choice.get('finish_reason') for choice in value.get('choices', []) if isinstance(choice, dict))


def is_business_event(value):
    if not isinstance(value, dict) or error_status(value) is not None or value.get('done'):
        return False
    kind = value.get('type') or value.get('event_type') or ''
    if kind in ('ping', 'response.created', 'response.in_progress', 'message_start', 'interaction.start', 'interaction.created', 'interaction.status_update'):
        return False
    if kind in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.complete'):
        return True
    if kind == 'response.output_item.added':
        item = value.get('item') or {}
        return item.get('type') not in ('message', 'reasoning') or bool(item.get('encrypted_content') or item.get('content') or item.get('summary'))
    if kind == 'content_block_start':
        block = value.get('content_block') or {}
        return block.get('type') not in ('text', 'thinking') or bool(block.get('text') or block.get('thinking'))
    if kind in ('response.content_part.added', 'response.reasoning_summary_part.added', 'response.reasoning_part.added'):
        part = value.get('part') or {}
        return bool(part.get('text') or part.get('refusal'))
    if kind == 'content_block_stop':
        return False
    if kind == 'content_block_delta':
        delta = value.get('delta') or {}
        return any(delta.get(key) for key in ('text', 'thinking', 'partial_json', 'signature'))
    if 'choices' in value:
        for choice in value.get('choices') or []:
            delta = choice.get('delta') or choice.get('message') or {}
            if any(delta.get(key) for key in ('content', 'reasoning_content', 'reasoning', 'reasoning_details', 'tool_calls', 'function_call', 'refusal', 'audio')):
                return True
        return False
    if 'candidates' in value:
        return any(any(part.get(key) for key in ('text', 'thoughtSignature', 'functionCall', 'inlineData', 'fileData', 'executableCode', 'codeExecutionResult'))
                   for candidate in value.get('candidates') or [] for part in (candidate.get('content') or {}).get('parts') or [] if isinstance(part, dict))
    if kind.endswith('.delta'):
        return bool(value.get('delta'))
    if kind:
        # Unknown typed provider/tool events commit the stream conservatively.
        return True
    return bool(value.get('content') or value.get('output') or value.get('steps') or value.get('promptFeedback'))
