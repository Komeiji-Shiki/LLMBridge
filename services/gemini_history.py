"""按历史前缀恢复 Gemini 签名，保留上游内容块及其顺序。

客户端可以省略思考和签名，或裁剪文本首尾空白；匹配后恢复原始模型轮次，
确保签名仍在签发它的内容块上。媒体、工具调用和工具结果参与历史匹配。
"""
import asyncio
import copy
import hashlib
import json
import logging

from core.conversation_store import conversation_store
from core.request_context import endpoint_identity

logger = logging.getLogger(__name__)

# 旧缓存没有保留签名所在的内容块，不能沿用其记录。
VERSION = 'gemini-thoughts-v2'


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _try_parse(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _canonical_args(args):
    if isinstance(args, str):
        stripped = args.strip()
        parsed = _try_parse(stripped)
        return _canonical(parsed) if parsed is not None else stripped
    return _canonical(args)


def _normalize_text(value):
    return value.replace('\r\n', '\n').replace('\r', '\n') if isinstance(value, str) else ''


def _text_part(part):
    return (isinstance(part.get('text'), str)
            and not (part.keys() - {'text', 'thought', 'thoughtSignature'}))


def normalize_turn(item):
    """只忽略思考与签名，保留媒体、工具 ID 和不同类型内容块的顺序。"""
    if isinstance(item, str):
        item = {'parts': [{'text': item}]}
    if not isinstance(item, dict):
        item = {}
    role = item.get('role') or 'user'
    if role == 'assistant':
        role = 'model'
    parts = item.get('parts')
    if isinstance(parts, str):
        parts = [{'text': parts}]
    visible = []
    for part in parts if isinstance(parts, list) else []:
        if not isinstance(part, dict) or part.get('thought'):
            continue
        content = {key: copy.deepcopy(value) for key, value in part.items()
                   if key not in ('thoughtSignature', 'thought')}
        if not content:
            continue
        if _text_part(content):
            text = _normalize_text(content['text'])
            if not text:
                continue
            if visible and _text_part(visible[-1]):
                visible[-1]['text'] += text
            else:
                visible.append({'text': text})
            continue
        call = content.get('functionCall')
        if isinstance(call, dict):
            call['args'] = _canonical_args(call.get('args'))
        visible.append(content)
    if role == 'model':
        for part in visible:
            if _text_part(part):
                part['text'] = part['text'].strip()
        visible = [part for part in visible if not _text_part(part) or part['text']]
    return {'role': role, 'parts': visible}


def _advance(digest, item):
    encoded = _canonical(normalize_turn(item)).encode()
    digest.update(len(encoded).to_bytes(8, 'big'))
    digest.update(encoded)


def history_boundaries(contents):
    """生成每个模型轮次结束时的 (index, digest)。"""
    digest = hashlib.sha256(VERSION.encode())
    for index, item in enumerate(contents):
        _advance(digest, item)
        if isinstance(item, dict) and item.get('role') in ('model', 'assistant'):
            yield index, digest.hexdigest()


def _args_complete(args):
    if args is None:
        return False
    return bool(args.strip()) and _try_parse(args) is not None if isinstance(args, str) else True


def _merge_args(existing, incoming):
    if incoming is None:
        return copy.deepcopy(existing)
    if existing is None:
        return copy.deepcopy(incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        return {**existing, **incoming}
    if isinstance(existing, str) and isinstance(incoming, str):
        joined = existing + incoming
        parsed = _try_parse(joined)
        return parsed if parsed is not None else joined
    if isinstance(existing, str) and isinstance(incoming, dict):
        parsed = _try_parse(existing)
        return {**parsed, **incoming} if isinstance(parsed, dict) else copy.deepcopy(incoming)
    if isinstance(existing, dict) and isinstance(incoming, str):
        return copy.deepcopy(existing)
    return copy.deepcopy(incoming)


class _CandidateTurn:
    """单个候选的增量组装；有签名的文本块始终保持原来的边界。"""

    def __init__(self):
        self.parts = []
        self.finish_reason = None

    def feed(self, part):
        part = copy.deepcopy(part)
        last = self.parts[-1] if self.parts else None
        call = part.get('functionCall')
        previous = last.get('functionCall') if last else None
        if isinstance(call, dict) and isinstance(previous, dict):
            same_id = call.get('id') is not None and call['id'] == previous.get('id')
            continuation = (call.get('id') is None and previous.get('id') is None
                            and (not previous.get('name') or not call.get('name')
                                 or previous['name'] == call['name'])
                            and not _args_complete(previous.get('args')))
            if same_id or continuation:
                args = _merge_args(previous.get('args'), call.get('args'))
                previous.update(call)
                previous['args'] = args
                last.update({key: value for key, value in part.items() if key != 'functionCall'})
                return
        if (last and _text_part(last) and _text_part(part)
                and bool(last.get('thought')) == bool(part.get('thought'))
                and not last.get('thoughtSignature') and not part.get('thoughtSignature')):
            last['text'] += part['text']
            return
        self.parts.append(part)


class GeminiTurnAssembler:
    """分别组装每个候选，只有明确 STOP 的完整轮次可以进入签名缓存。"""

    def __init__(self):
        self.candidates = {}

    def feed(self, value):
        candidates = value.get('candidates') if isinstance(value, dict) else None
        if not isinstance(candidates, list):
            return
        for position, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                continue
            index = candidate.get('index', position)
            turn = self.candidates.setdefault(index, _CandidateTurn())
            content = candidate.get('content')
            parts = content.get('parts') if isinstance(content, dict) else None
            for part in parts if isinstance(parts, list) else []:
                if isinstance(part, dict):
                    turn.feed(part)
            if candidate.get('finishReason'):
                turn.finish_reason = candidate['finishReason']

    def completed_parts(self):
        for turn in self.candidates.values():
            if turn.finish_reason == 'STOP' and any(part.get('thoughtSignature') for part in turn.parts):
                yield turn.parts

    def has_signatures(self):
        return any(self.completed_parts())


def _patch_turn(item, record):
    """匹配整轮后恢复原始内容块，避免签名移位或重复签署相同工具调用。"""
    if not isinstance(item, dict) or record.get('v') != 2:
        return 0
    parts = item.get('parts')
    if isinstance(parts, str):
        parts = [{'text': parts}]
    stored = record.get('parts')
    if not isinstance(parts, list) or not isinstance(stored, list):
        return 0
    if any(isinstance(part, dict) and part.get('thoughtSignature') for part in parts):
        return 0
    if normalize_turn(item) != normalize_turn({'role': 'model', 'parts': stored}):
        return 0
    thoughts = [part for part in parts if isinstance(part, dict) and part.get('thought')]
    if thoughts:
        original = [part for part in stored if part.get('thought')]
        # 先拼接再裁剪，不能删除分片之间属于正文的空格。
        text = _normalize_text(''.join(part.get('text', '') for part in thoughts)).strip()
        expected = _normalize_text(''.join(part.get('text', '') for part in original)).strip()
        if text and text != expected:
            return 0
        nontext = [{key: value for key, value in part.items() if key != 'thoughtSignature'}
                   for part in thoughts if not _text_part(part)]
        if nontext and nontext != [{key: value for key, value in part.items() if key != 'thoughtSignature'}
                                  for part in original if not _text_part(part)]:
            return 0
    restored = sum(bool(part.get('thoughtSignature')) for part in stored)
    if restored:
        item['parts'] = copy.deepcopy(stored)
    return restored


class GeminiThoughtHistory:
    """按调用方、端点和可见历史保存与恢复原生 Gemini 签名。"""

    def __init__(self, context, endpoint_config, request_body):
        self.context = context
        self.endpoint_config = endpoint_config if isinstance(endpoint_config, dict) else {}
        self.request_body = request_body if isinstance(request_body, dict) else {}

    def scope(self):
        from services.provider_capabilities import apply_native_tool_defaults
        template = {key: copy.deepcopy(self.request_body[key])
                    for key in ('systemInstruction', 'tools', 'toolConfig', 'cachedContent')
                    if key in self.request_body}
        template = apply_native_tool_defaults(template, self.endpoint_config)
        template['model'] = self.endpoint_config.get('model_id') or ''
        value = [VERSION, self.context.model, endpoint_identity(self.context.endpoint),
                 self.context.credential_fingerprint,
                 self.context.session_id if self.context.explicit_session else None, template]
        return hashlib.sha256(_canonical(value).encode()).hexdigest()

    async def restore(self, upstream_body):
        if not self.context.authenticated or not self.context.owner_id:
            return 0
        contents = upstream_body.get('contents') if isinstance(upstream_body, dict) else None
        if not isinstance(contents, list) or not contents:
            return 0
        boundaries = list(history_boundaries(contents))
        if not boundaries:
            return 0
        try:
            records = await asyncio.to_thread(conversation_store.response_prefixes,
                                              self.context.owner_id, self.scope(),
                                              [digest for _, digest in boundaries])
        except Exception:
            logger.warning('Gemini 思考签名查询失败；按客户端原始内容请求上游')
            return 0
        restored = 0
        for index, digest in boundaries:
            record = records.get(digest)
            if isinstance(record, dict):
                restored += _patch_turn(contents[index], record)
        return restored

    async def remember(self, contents, assembler):
        if not self.context.authenticated or not self.context.owner_id or not isinstance(contents, list):
            return
        if not isinstance(assembler, GeminiTurnAssembler) or not assembler.has_signatures():
            return
        digest = hashlib.sha256(VERSION.encode())
        for item in contents:
            _advance(digest, item)
        scope = self.scope()
        try:
            for parts in assembler.completed_parts():
                prefix = digest.copy()
                _advance(prefix, {'role': 'model', 'parts': parts})
                await asyncio.to_thread(conversation_store.remember_response_prefix,
                                        self.context.owner_id, scope, prefix.hexdigest(),
                                        {'v': 2, 'parts': parts})
        except Exception:
            logger.warning('Gemini 思考签名保存失败；下一轮将按客户端原始内容请求上游')
