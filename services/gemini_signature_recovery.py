"""记录 GenerateContent 端点拒绝过的可选文本签名组合。"""

from hashlib import blake2b

from cachetools import LRUCache


_SIGNATURE_FIELDS = {'thoughtSignature', 'thought_signature'}
_TEXT_FIELDS = _SIGNATURE_FIELDS | {'text', 'thought'}
# 按签名摘要总数限制进程内缓存，命中的组合会保留得更久。
_rejected_groups = LRUCache(maxsize=4096, getsizeof=len)


def _signed_text_parts(body):
    contents = body.get('contents')
    if not isinstance(contents, list):
        return {}
    signed = {}
    for turn_index, turn in enumerate(contents):
        if not isinstance(turn, dict) or turn.get('role') != 'model':
            continue
        parts = turn.get('parts')
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if not isinstance(part, dict) or part.keys() - _TEXT_FIELDS:
                continue
            fields = {key: blake2b(value.encode(), digest_size=16).digest()
                      for key, value in part.items() if key in _SIGNATURE_FIELDS and isinstance(value, str) and value}
            if fields:
                signed[turn_index, part_index] = fields
    return signed


def _signature_group(signed_parts):
    return frozenset(signature for fields in signed_parts.values() for signature in fields.values())


def _without_signatures(body, signed_parts, rejected):
    contents = []
    for turn_index, turn in enumerate(body['contents']):
        parts = turn.get('parts') if isinstance(turn, dict) else None
        if not isinstance(parts, list) or not parts:
            contents.append(turn)
            continue
        kept = []
        for part_index, part in enumerate(parts):
            fields = signed_parts.get((turn_index, part_index), {})
            omitted = {key for key, signature in fields.items() if signature in rejected}
            if omitted:
                part = {key: value for key, value in part.items() if key not in omitted}
                # 流式签名有时单独附在空文本块上，移除签名后一并去掉空块。
                if not part.get('text') and not part.keys() & _SIGNATURE_FIELDS:
                    continue
            kept.append(part)
        if kept:
            contents.append({**turn, 'parts': kept})
    return {**body, 'contents': contents}


class GeminiTextSignatureRecovery:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.sent_body = {}
        self.retry_group = frozenset()
        self.omitted = frozenset()

    def prepare(self, body):
        self.sent_body = body
        self.omitted = frozenset()
        known = [key for key in _rejected_groups if key[0] == self.endpoint]
        if not known and not self.retry_group:
            return body
        signed_parts = _signed_text_parts(body)
        signatures = _signature_group(signed_parts)
        omitted = self.retry_group & signatures
        for key in known:
            if key[1] <= signatures:
                omitted |= _rejected_groups[key]
        if omitted:
            self.omitted = omitted
            self.sent_body = _without_signatures(body, signed_parts, omitted)
        return self.sent_body

    def plan_retry(self):
        if not self.retry_group:
            self.retry_group = _signature_group(_signed_text_parts(self.sent_body))
            return len(self.retry_group)
        return 0

    def confirm(self):
        # 上游没有指出具体哪条签名坏了，只记录成功移除的组合。
        group = self.retry_group
        if group and group <= self.omitted and len(group) <= _rejected_groups.maxsize:
            _rejected_groups[self.endpoint, group] = group
