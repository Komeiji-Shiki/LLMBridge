"""按可见历史前缀保存并恢复原生 Gemini 请求缺失的思考签名。

响应侧把已完成模型轮次的带签名思考与工具调用记录到会话存储；请求侧用同样的
可见历史指纹匹配并回填签名。客户端无需回传 thoughtSignature，编辑过的文本
永远不会获得签名。指纹与响应式上游一致：思考、签名不参与匹配，模型正文统一
首尾空白与 CRLF/LF，用户消息保持原样。
"""
import asyncio
import copy
import hashlib
import json
import logging

from core.conversation_store import conversation_store
from core.request_context import endpoint_identity

logger = logging.getLogger(__name__)

VERSION = 'gemini-thoughts-v1'


def _canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return json.dumps(str(value), ensure_ascii=False)


def _try_parse(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _canonical_args(args):
    if isinstance(args, str):
        stripped = args.strip()
        if not stripped:
            return ''
        parsed = _try_parse(stripped)
        return _canonical(parsed) if parsed is not None else stripped
    return _canonical(args)


def _normalize_text(value):
    if not isinstance(value, str):
        return ''
    return value.replace('\r\n', '\n').replace('\r', '\n')


def normalize_turn(item):
    """把一条 contents 记录归一化成指纹用的稳定结构。

    只保留客户端会原样回传的部分：角色、正文、工具调用与工具结果。
    思考、签名、媒体及其他不属于回传协议的内容不参与指纹。
    """
    if isinstance(item, str):
        return {'role': 'user', 'text': _normalize_text(item), 'calls': [], 'results': []}
    if not isinstance(item, dict):
        return {'role': 'user', 'text': '', 'calls': [], 'results': []}
    role = item.get('role') or 'user'
    if role == 'assistant':
        role = 'model'
    parts = item.get('parts')
    if isinstance(parts, str):
        parts = [{'text': parts}]
    if not isinstance(parts, list):
        parts = []
    text_chunks = []
    calls = []
    results = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get('thought'):
            continue
        if 'functionCall' in part:
            call = part.get('functionCall') if isinstance(part.get('functionCall'), dict) else {}
            calls.append([str(call.get('name') or ''), _canonical_args(call.get('args'))])
            continue
        if 'functionResponse' in part:
            response = part.get('functionResponse') if isinstance(part.get('functionResponse'), dict) else {}
            results.append([str(response.get('name') or ''), _canonical(response.get('response'))])
            continue
        text = part.get('text')
        if isinstance(text, str) and text:
            text_chunks.append(text)
    joined = _normalize_text(''.join(text_chunks))
    if role == 'model':
        joined = joined.strip()
    return {'role': role, 'text': joined, 'calls': calls, 'results': results}


def _advance(digest, item):
    encoded = _canonical(normalize_turn(item)).encode()
    digest.update(len(encoded).to_bytes(8, 'big'))
    digest.update(encoded)


def history_boundaries(contents):
    """生成 (index, digest)：每个模型轮次结束时的可见历史指纹。"""
    digest = hashlib.sha256(VERSION.encode())
    for index, item in enumerate(contents):
        _advance(digest, item)
        role = item.get('role') if isinstance(item, dict) else None
        if role in ('model', 'assistant'):
            yield index, digest.hexdigest()


def _args_complete(args):
    if args is None:
        return False
    if isinstance(args, str):
        return bool(args.strip()) and _try_parse(args) is not None
    return True


def _merge_args(existing, incoming):
    if incoming is None:
        return copy.deepcopy(existing)
    if existing is None:
        return copy.deepcopy(incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        merged = dict(existing)
        merged.update(incoming)
        return merged
    if isinstance(existing, str) and isinstance(incoming, str):
        joined = existing + incoming
        parsed = _try_parse(joined)
        return parsed if parsed is not None else joined
    if isinstance(existing, str) and isinstance(incoming, dict):
        parsed = _try_parse(existing)
        if isinstance(parsed, dict):
            parsed.update(incoming)
            return parsed
        return copy.deepcopy(incoming)
    if isinstance(existing, dict) and isinstance(incoming, str):
        return copy.deepcopy(existing)
    return copy.deepcopy(incoming)


class GeminiTurnAssembler:
    """从流式或非流式 Gemini 响应的 parts 中拼出完整的模型轮次。"""

    def __init__(self):
        self.thought_fragments = []
        self.calls = []
        self.trailing = []
        self.text_chunks = []
        self._signables = []
        self._new_fragment = False

    def feed(self, value):
        if not isinstance(value, dict):
            return
        candidates = value.get('candidates')
        if not isinstance(candidates, list):
            return
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get('content')
            parts = content.get('parts') if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            for part in parts:
                if isinstance(part, dict):
                    self._feed_part(part)

    def _feed_part(self, part):
        if 'functionCall' in part:
            self._feed_call(part)
            return
        if part.get('thought'):
            self._feed_thought(part)
            return
        text = part.get('text')
        if isinstance(text, str) and text:
            self.text_chunks.append(text)
            self._new_fragment = True
            return
        if part.get('thoughtSignature'):
            self._attach_signature(part['thoughtSignature'])

    def _feed_thought(self, part):
        if self._new_fragment or not self.thought_fragments:
            fragment = {'text': '', 'signature': None}
            self.thought_fragments.append(fragment)
            self._signables.append(fragment)
            self._new_fragment = False
        fragment = self.thought_fragments[-1]
        text = part.get('text')
        if isinstance(text, str):
            fragment['text'] += text
        signature = part.get('thoughtSignature')
        if isinstance(signature, str) and signature:
            fragment['signature'] = signature

    def _feed_call(self, part):
        call = part.get('functionCall') if isinstance(part.get('functionCall'), dict) else {}
        identifier = call.get('id')
        name = str(call.get('name') or '')
        target = None
        if self.calls:
            last = self.calls[-1]
            same_id = identifier is not None and last.get('id') == identifier
            no_id_continuation = (identifier is None and last.get('id') is None
                                  and (not last.get('name') or not name or last.get('name') == name)
                                  and not _args_complete(last.get('args')))
            if same_id or no_id_continuation:
                target = last
        if target is None:
            target = {'id': identifier, 'name': name, 'args': None, 'signature': None}
            self.calls.append(target)
            self._signables.append(target)
        elif not target.get('name'):
            target['name'] = name
        target['args'] = _merge_args(target.get('args'), call.get('args'))
        signature = part.get('thoughtSignature')
        if isinstance(signature, str) and signature:
            target['signature'] = signature
        self._new_fragment = True

    def _attach_signature(self, signature):
        if not isinstance(signature, str) or not signature:
            return
        for candidate in reversed(self._signables):
            if not candidate.get('signature'):
                candidate['signature'] = signature
                return
        self.trailing.append(signature)

    def signed_thoughts(self):
        return [fragment for fragment in self.thought_fragments
                if fragment.get('text') and fragment.get('signature')]

    def signed_calls(self):
        return [call for call in self.calls
                if call.get('signature') and (call.get('name') or call.get('id') is not None)]

    def has_signatures(self):
        return bool(self.signed_thoughts() or self.signed_calls() or self.trailing)

    def record(self):
        thoughts = [{'text': fragment['text'], 'signature': fragment['signature']}
                    for fragment in self.signed_thoughts()]
        calls = []
        for call in self.signed_calls():
            calls.append({'id': call.get('id'), 'name': call.get('name') or '',
                          'args': _canonical_args(call.get('args')), 'signature': call['signature']})
        return {'v': 1, 'thoughts': thoughts, 'calls': calls, 'trailing': list(self.trailing)}

    def assembled_parts(self):
        parts = []
        for fragment in self.thought_fragments:
            part = {'thought': True, 'text': fragment.get('text') or ''}
            if fragment.get('signature'):
                part['thoughtSignature'] = fragment['signature']
            parts.append(part)
        if self.text_chunks:
            parts.append({'text': ''.join(self.text_chunks)})
        for call in self.calls:
            function_call = {'name': call.get('name') or '', 'args': call.get('args')}
            if call.get('id') is not None:
                function_call['id'] = call['id']
            part = {'functionCall': function_call}
            if call.get('signature'):
                part['thoughtSignature'] = call['signature']
            parts.append(part)
        return parts


def _patch_turn(item, record):
    """把一条匹配到的模型轮次按记录回填签名；返回回填数量。"""
    if not isinstance(item, dict):
        return 0
    parts = item.get('parts')
    if isinstance(parts, str):
        parts = [{'text': parts}]
        item['parts'] = parts
    if not isinstance(parts, list) or not parts:
        return 0
    for part in parts:
        if isinstance(part, dict) and part.get('thoughtSignature'):
            return 0
    restored = 0
    thoughts = record.get('thoughts') if isinstance(record.get('thoughts'), list) else []
    signed_thoughts = [fragment for fragment in thoughts
                       if isinstance(fragment, dict)
                       and isinstance(fragment.get('text'), str) and fragment.get('text')
                       and isinstance(fragment.get('signature'), str) and fragment.get('signature')]
    if signed_thoughts:
        client_thoughts = [part for part in parts
                           if isinstance(part, dict) and part.get('thought')
                           and isinstance(part.get('text'), str) and part.get('text')]
        if client_thoughts:
            client_text = ''.join(part['text'] for part in client_thoughts)
            stored_text = ''.join(fragment['text'] for fragment in signed_thoughts)
            if client_text == stored_text:
                if len(signed_thoughts) == 1:
                    client_thoughts[-1]['thoughtSignature'] = signed_thoughts[0]['signature']
                    restored += 1
                elif len(signed_thoughts) == len(client_thoughts):
                    if all(part['text'] == fragment['text']
                           for part, fragment in zip(client_thoughts, signed_thoughts)):
                        for part, fragment in zip(client_thoughts, signed_thoughts):
                            part['thoughtSignature'] = fragment['signature']
                        restored += len(signed_thoughts)
        else:
            insert = [{'thought': True, 'text': fragment['text'],
                       'thoughtSignature': fragment['signature']} for fragment in signed_thoughts]
            parts[:0] = insert
            restored += len(insert)
    calls = record.get('calls') if isinstance(record.get('calls'), list) else []
    signed_calls = [call for call in calls
                    if isinstance(call, dict) and isinstance(call.get('signature'), str) and call.get('signature')]
    for part in parts:
        if not isinstance(part, dict) or part.get('thoughtSignature') or 'functionCall' not in part:
            continue
        function_call = part.get('functionCall') if isinstance(part.get('functionCall'), dict) else {}
        for stored in signed_calls:
            if stored.get('id') is not None and function_call.get('id') is not None:
                matched = stored.get('id') == function_call.get('id')
            else:
                matched = (str(stored.get('name') or '') == str(function_call.get('name') or '')
                           and stored.get('args') == _canonical_args(function_call.get('args')))
            if matched:
                part['thoughtSignature'] = stored['signature']
                restored += 1
                break
    trailing = record.get('trailing') if isinstance(record.get('trailing'), list) else []
    if trailing and not restored and not signed_thoughts and not signed_calls:
        for signature in trailing:
            if isinstance(signature, str) and signature:
                parts.append({'thoughtSignature': signature, 'text': ''})
                restored += 1
    return restored


class GeminiThoughtHistory:
    """按可见历史指纹保存并恢复原生 Gemini 轮次的思考签名。"""

    def __init__(self, context, endpoint_config, request_body):
        self.context = context
        self.endpoint_config = endpoint_config if isinstance(endpoint_config, dict) else {}
        self.request_body = request_body if isinstance(request_body, dict) else {}

    def scope(self):
        from services.provider_capabilities import apply_native_tool_defaults
        template_body = {key: self.request_body[key] for key in ('systemInstruction', 'tools')
                         if key in self.request_body}
        effective = apply_native_tool_defaults(copy.deepcopy(template_body), self.endpoint_config)
        template = {'model': self.endpoint_config.get('model_id') or '',
                    'system': effective.get('systemInstruction'),
                    'tools': effective.get('tools')}
        value = [VERSION, self.context.model, endpoint_identity(self.context.endpoint),
                 self.context.credential_fingerprint,
                 self.context.session_id if self.context.explicit_session else None,
                 template]
        return hashlib.sha256(_canonical(value).encode()).hexdigest()

    async def restore(self, upstream_body):
        """把匹配到的签名回填到 upstream_body 的 contents；返回回填数量。"""
        if not self.context.authenticated or not self.context.owner_id:
            return 0
        contents = upstream_body.get('contents') if isinstance(upstream_body, dict) else None
        if not isinstance(contents, list) or not contents:
            return 0
        boundaries = list(history_boundaries(contents))
        if not boundaries:
            return 0
        prefixes = list(dict.fromkeys(digest for _, digest in boundaries))
        try:
            records = await asyncio.to_thread(conversation_store.response_prefixes,
                                              self.context.owner_id, self.scope(), prefixes)
        except Exception:
            logger.warning('Gemini 思考签名查询失败；按客户端原始内容请求上游')
            return 0
        if not records:
            return 0
        restored = 0
        for index, digest in boundaries:
            record = records.get(digest)
            if not isinstance(record, dict):
                continue
            try:
                restored += _patch_turn(contents[index], record)
            except Exception:
                logger.warning('Gemini 思考签名回填失败；跳过该轮次')
        return restored

    async def remember(self, contents, assembler):
        """保存刚完成轮次的签名记录；contents 为客户端原始轮次列表。"""
        if not self.context.authenticated or not self.context.owner_id:
            return
        if not isinstance(assembler, GeminiTurnAssembler) or not assembler.has_signatures():
            return
        if not isinstance(contents, list):
            return
        digest = hashlib.sha256(VERSION.encode())
        for item in contents:
            _advance(digest, item)
        _advance(digest, {'role': 'model', 'parts': assembler.assembled_parts()})
        prefix = digest.hexdigest()
        try:
            await asyncio.to_thread(conversation_store.remember_response_prefix,
                                    self.context.owner_id, self.scope(), prefix, assembler.record())
        except Exception:
            logger.warning('Gemini 思考签名保存失败；下一轮将按客户端原始内容请求上游')
