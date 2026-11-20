"""Classify protocol events without changing their wire representation."""
from collections import deque
from http import HTTPStatus
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


def upstream_response_model(values):
    """从原始响应或流式事件中取模型标识，不从请求或回复正文推断。"""
    for event in reversed(values):
        if not isinstance(event, dict) or event.get('error'):
            continue
        for response in (event.get('response'), event.get('message'), event.get('interaction'), event):
            if not isinstance(response, dict):
                continue
            for field in ('modelVersion', 'model_version', 'model', 'model_id'):
                model = response.get(field)
                if isinstance(model, str) and model.strip():
                    return model.strip()
    return None


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


# Interactions 因内容被拦而失败时的错误码；重试只会得到同样的结果，按请求错误处理。
INTERACTIONS_BLOCKED_CODES = frozenset({'safety', 'jailbreak', 'prohibited_content', 'blocklist'})

# 字符串错误标识的唯一映射表。各家写法不同：Interactions 用 HTTP 状态名（too_many_requests），
# Gemini 用 google.rpc 规范码（RESOURCE_EXHAUSTED），OpenAI 和 Anthropic 用错误类型（overloaded_error）。
NAMED_ERROR_STATUS = {
    **{name.lower(): status.value for name, status in HTTPStatus.__members__.items() if status >= 400},
    'invalid_argument': 400, 'failed_precondition': 400, 'out_of_range': 400, 'unauthenticated': 401,
    'permission_denied': 403, 'already_exists': 409, 'aborted': 409, 'resource_exhausted': 429,
    'internal': 500, 'unknown': 500, 'data_loss': 500, 'unimplemented': 501, 'unavailable': 503,
    'deadline_exceeded': 504,
    'invalid_request_error': 400, 'authentication_error': 401, 'permission_error': 403, 'not_found_error': 404,
    'rate_limit_error': 429, 'rate_limit_exceeded': 429,
    'server_error': 503, 'service_unavailable_error': 503, 'overloaded_error': 503,
    **dict.fromkeys(INTERACTIONS_BLOCKED_CODES, 400),
}


def named_error_status(value):
    """数字或字符串错误标识折算成 HTTP 错误状态码，认不出返回 None。"""
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return NAMED_ERROR_STATUS.get(value.lower())
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if 400 <= value <= 599 else None


def interaction_error(interaction):
    """失败或取消的 Interaction 取第一条错误（没有时为空字典），其他状态返回 None。"""
    if not isinstance(interaction, dict) or interaction.get('status') not in ('failed', 'cancelled'):
        return None
    errors = interaction.get('errors')
    return errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}


def error_status(value):
    if not isinstance(value, dict):
        return None
    error = value.get('error')
    kind = value.get('type') or value.get('event_type') or ''
    if kind == 'response.failed':
        error = (value.get('response') or {}).get('error') or {'code': 502}
    elif kind == 'interaction.completed':
        error = interaction_error(value.get('interaction'))
    if error is None and kind != 'error':
        return None
    error = error if isinstance(error, dict) else {}
    for code in (value.get('_http_status'), error.get('code'), error.get('status'), error.get('type'), value.get('code')):
        if status := named_error_status(code):
            return status
    kind = str(error.get('type') or value.get('code') or '')
    return 429 if 'rate_limit' in kind else 504 if 'timeout' in kind else 502


def is_terminal(value):
    if not isinstance(value, dict):
        return False
    if (value.get('promptFeedback') or {}).get('blockReason'):
        return True
    if value.get('done') or (value.get('type') or value.get('event_type')) in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.completed'):
        return True
    if any(candidate.get('finishReason') for candidate in value.get('candidates', []) if isinstance(candidate, dict)):
        return True
    return any(choice.get('finish_reason') for choice in value.get('choices', []) if isinstance(choice, dict))


def is_business_event(value):
    if not isinstance(value, dict) or error_status(value) is not None or value.get('done'):
        return False
    kind = value.get('type') or value.get('event_type') or ''
    if kind in ('ping', 'response.created', 'response.in_progress', 'message_start', 'interaction.created', 'interaction.status_update'):
        return False
    if kind in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.completed'):
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
    if kind == 'step.start':
        # Interactions 的文本和思考步骤先发空壳，内容随后在 step.delta 里到达。
        step = value.get('step') or {}
        return step.get('type') not in ('model_output', 'thought') or bool(step.get('content') or step.get('summary') or step.get('signature'))
    if kind in ('content_block_stop', 'step.stop'):
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


# 三家流式协议里“一个步骤/内容块”的起止事件
_BLOCK_START = frozenset(('step.start', 'content_block_start', 'response.output_item.added'))
_BLOCK_STOP = frozenset(('step.stop', 'content_block_stop', 'response.output_item.done'))
_UNFINISHED_TAIL = 8
# 签名和 base64 动辄上万字符，快照只留开头，结构和其余字段原样保留
_CLIP_CHARS = 1000
_CLIP_ITEMS = 20


class UnfinishedBlock:
    """跟踪最近一个还没收尾的步骤或内容块：起始事件、增量个数和最后几个增量。

    只存原始事件引用、不拼内容，每个事件的开销是一次判断加一次追加。
    """

    def __init__(self):
        self.start = None
        self.deltas = 0
        self.tail = deque(maxlen=_UNFINISHED_TAIL)

    def feed(self, value):
        kind = (value.get('event_type') or value.get('type')) if isinstance(value, dict) else None
        if kind in _BLOCK_START:
            self.start, self.deltas = value, 0
            self.tail.clear()
        elif kind in _BLOCK_STOP:
            self.start = None
            self.tail.clear()
        elif self.start is not None:
            self.deltas += 1
            self.tail.append(value)

    def snapshot(self):
        if self.start is None:
            return None
        return {'start': self.start, 'deltas': self.deltas, 'last_deltas': list(self.tail)}


def failure_snapshot(event, unfinished=None):
    """上游失败时的原始事件，连同当时没写完的块，长字符串和长列表截短后留作诊断。"""
    snapshot = {'event': _clip(event)}
    if unfinished:
        snapshot['unfinished'] = _clip(unfinished)
    return snapshot


def _clip(value):
    if isinstance(value, str):
        return value if len(value) <= _CLIP_CHARS else f'{value[:_CLIP_CHARS]}…（共 {len(value)} 字符）'
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in value.items()}
    if isinstance(value, list):
        clipped = [_clip(item) for item in value[:_CLIP_ITEMS]]
        if len(value) > _CLIP_ITEMS:
            clipped.append(f'…（共 {len(value)} 项）')
        return clipped
    return value
