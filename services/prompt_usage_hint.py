"""上游只在流尾报用量时，给 Anthropic message_start 估一个输入量。

官方 message_start.usage 已带完整输入量。Claude Code 在每个内容块结束时就把消息
转给宿主，message_delta 的最终用量只回写到内存和转录，不会再发一次；Claude
桌面端的上下文用量按最近一条输入量非零的消息显示，message_start 为 0 时会一直
停在很久以前的旧值上。

估法：请求结束时按「模型 + 消息前缀」记下真实输入量。下一次请求是同一前缀加上
助手回复和工具结果，估计值 = 记下的真实输入量 + 追加部分的粗估。提示词缓存
要求前缀逐字节相同，去掉每轮挪位置的 cache_control 后，能命中缓存的请求一定
能对上前缀；对不上（回退、改写历史、新会话）时不给估计。

估计值全部放在 input_tokens：Claude Code 合并 message_delta 时只用大于 0 的字段
覆盖，缓存字段填了估计值而最终没有命中缓存时会残留下来。
"""
import hashlib
import json
import threading

from cachetools import LRUCache

from core.request_context import current_request
from utils.usage_tokens import rough_token_count

_known_inputs = LRUCache(maxsize=4096)
_lock = threading.Lock()


def _without_cache_control(value):
    if isinstance(value, dict):
        return {key: _without_cache_control(item) for key, item in value.items() if key != 'cache_control'}
    if isinstance(value, list):
        return [_without_cache_control(item) for item in value]
    return value


def _framed(message):
    raw = json.dumps(_without_cache_control(message), ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return len(raw).to_bytes(8, 'big') + raw.encode()


def _block_text(block):
    if not isinstance(block, dict):
        return ''
    kind = block.get('type')
    if kind == 'text':
        return block.get('text') or ''
    if kind == 'tool_use':
        return json.dumps(block.get('input') or {}, ensure_ascii=False)
    if kind == 'tool_result':
        content = block.get('content')
        if isinstance(content, list):
            return ''.join(_block_text(item) for item in content)
        return content if isinstance(content, str) else ''
    # 思考只回传签名，不按原文计入输入；图片和文件没有可用的粗估
    return ''


def _rough_tokens(message):
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return rough_token_count(content)
    if isinstance(content, list):
        return sum(rough_token_count(_block_text(block)) for block in content)
    return 0


class PromptUsageHint:
    """一次流式请求的输入量估计；构造时计算前缀摘要，较大的历史应放在线程里构造。"""

    def __init__(self, model, messages):
        context = current_request.get()
        owner = context.owner_id if context and context.owner_id else ''
        digest = hashlib.sha256(json.dumps(['prompt-usage-v1', model, owner]).encode())
        boundaries = []
        for message in messages:
            digest.update(_framed(message))
            boundaries.append(digest.hexdigest())
        self._key = boundaries[-1] if boundaries else None
        self.input_tokens = 0
        with _lock:
            for index in range(len(boundaries) - 1, -1, -1):
                known = _known_inputs.get(boundaries[index])
                if known:
                    self.input_tokens = known + sum(_rough_tokens(item) for item in messages[index + 1:])
                    break

    def remember(self, usage):
        """usage 为 Anthropic 格式的最终用量。"""
        total = sum(int(usage.get(key) or 0) for key in
                    ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
        if self._key and total > 0:
            with _lock:
                _known_inputs[self._key] = total
