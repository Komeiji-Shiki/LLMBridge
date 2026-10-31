"""Gemini Long Decode 续传：隐藏中间跳的结束原因，把多跳合并成一次响应。

协议见 https://ai.google.dev/gemini-api/docs/generate-content/long-decode
- 每跳原样重发原始请求（maxOutputTokens 是跨跳总预算），只在顶层加最新 token；
- usageMetadata 在单跳流内累计、跨跳相加，每一跳都按完整提示计费。
"""
import logging

logger = logging.getLogger(__name__)

_OUTPUT_FIELDS = ('candidatesTokenCount', 'thoughtsTokenCount', 'toolUsePromptTokenCount')
# 明细只描述单跳，合并后与总量对不上
_HOP_ONLY_FIELDS = ('candidatesTokensDetails', 'cacheTokensDetails')


def _first_candidate(value):
    candidates = value.get('candidates')
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        return candidates[0]
    return None


def _count(usage, field):
    return int(usage.get(field) or 0)


class GeminiContinuation:
    """单次请求的续传状态。"""

    def __init__(self, max_hops):
        self.max_hops = max_hops
        self.hops = 0
        self._token = None
        self._last_token = None
        self._pending = False
        self._hop_usage = None
        self._totals = None
        self._parts = None

    def feed(self, value):
        """处理一个上游块：合并用量，可续传时去掉中间跳的结束原因。"""
        if not isinstance(value, dict):
            return
        usage = value.get('usageMetadata')
        if isinstance(usage, dict) and usage:
            self._hop_usage = usage
            if self._totals:
                value['usageMetadata'] = self._merged(usage)
        candidate = _first_candidate(value)
        if candidate is None:
            return
        # 流内 token 可能是检查点；其后又有新内容时再用它续传会重复生成
        if (candidate.get('content') or {}).get('parts'):
            self._token = None
        self._token = candidate.get('continuationToken') or self._token
        if candidate.get('finishReason') == 'CONTINUATION' and self._can_resume():
            del candidate['finishReason']
            self._pending = True

    def merge_response(self, response):
        """非流式：把此前各跳的 parts 接到本跳前面，返回合并后的响应。"""
        self.feed(response)
        candidate = _first_candidate(response)
        if candidate is None:
            return response
        if self._parts:
            content = candidate.setdefault('content', {'role': 'model'})
            content['parts'] = self._parts + (content.get('parts') or [])
        self._parts = (candidate.get('content') or {}).get('parts') or self._parts
        return response

    def next_hop(self):
        """本跳需要续传时返回下一跳的 token，否则返回 None。"""
        if not self._pending:
            return None
        self._pending = False
        self.hops += 1
        self._totals = self._add_hop(self._hop_usage or {})
        self._hop_usage = None
        self._last_token, self._token = self._token, None
        return self._last_token

    def with_usage(self, error):
        """续传中途失败时带上此前已计费的用量。"""
        if not self._totals:
            return error
        return {**error, 'usageMetadata': self._merged(self._hop_usage or {})}

    def _can_resume(self):
        if self.hops >= self.max_hops:
            logger.warning('[GEMINI_NATIVE] 已达到续传跳数上限 %s，按输出截断结束', self.max_hops)
            return False
        if not self._token or self._token == self._last_token:
            logger.warning('[GEMINI_NATIVE] 上游返回 CONTINUATION 但没有新的续传 token，按输出截断结束')
            return False
        return True

    def _add_hop(self, usage):
        totals = self._totals or {'prompt': 0, 'cached': 0, 'billed_prompt': 0, 'billed_cached': 0,
                                  **{field: 0 for field in _OUTPUT_FIELDS}}
        prompt, cached = _count(usage, 'promptTokenCount'), _count(usage, 'cachedContentTokenCount')
        return {'prompt': totals['prompt'] or prompt,
                'cached': totals['cached'] if totals['prompt'] else cached,
                'billed_prompt': totals['billed_prompt'] + prompt,
                'billed_cached': totals['billed_cached'] + cached,
                **{field: totals[field] + _count(usage, field) for field in _OUTPUT_FIELDS}}

    def _merged(self, usage):
        # 客户端用提示量判断上下文大小，所以只报第一跳；重复计费的部分单独记录。
        totals = self._add_hop(usage)
        merged = {key: value for key, value in usage.items() if key not in _HOP_ONLY_FIELDS}
        merged.update({field: totals[field] for field in _OUTPUT_FIELDS})
        merged.update(promptTokenCount=totals['prompt'], cachedContentTokenCount=totals['cached'],
                      totalTokenCount=totals['prompt'] + sum(totals[field] for field in _OUTPUT_FIELDS),
                      billedPromptTokenCount=totals['billed_prompt'], billedCachedTokenCount=totals['billed_cached'],
                      continuationHops=self.hops)
        return merged
