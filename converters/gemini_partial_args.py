"""Vertex AI 流式函数参数（streamFunctionCallArguments）的分片合并。

格式见 https://cloud.google.com/vertex-ai/generative-ai/docs/multimodal/function-calling
- 首片带 name 和 willContinue: true，后续分片没有 name，调用级 willContinue 为假或缺失
  的那片收尾（常见是一个空的 functionCall）；
- partialArgs 每项是 jsonPath 加 stringValue/numberValue/boolValue/nullValue 之一，
  字符串可以分多片，项内 willContinue 为真表示后面还有，收尾项可以只有 jsonPath。

原生客户端照收分片；签名历史和需要转换格式的链路先拼回完整调用。
"""
import copy
import re

_PATH_TOKEN = re.compile(r"\.([^.\[\]]+)|\[(\d+)\]|\['([^']*)'\]")
_VALUE_KEYS = ("stringValue", "numberValue", "boolValue", "nullValue")


def _path_keys(path):
    return [int(index) if index else name or quoted
            for name, index, quoted in _PATH_TOKEN.findall(path.removeprefix("$"))]


def _slot(container, key, value=None, create=False):
    """读取或写入 container[key]，列表按下标补齐。"""
    if isinstance(container, list):
        if len(container) <= key:
            container.extend([None] * (key + 1 - len(container)))
        if create:
            container[key] = value
        return container[key]
    if create:
        container[key] = value
    return container.get(key)


def _is_streaming(call):
    return isinstance(call, dict) and (call.get("willContinue") or "partialArgs" in call)


class _Call:
    """一个正在接收分片的调用。签名等 part 级字段可能出现在任意一片上。"""

    def __init__(self):
        self.part = {}
        self.call = {}
        self.args = {}
        self.open_strings = set()

    def add(self, part):
        call = part["functionCall"]
        self.part.update((key, value) for key, value in part.items() if key != "functionCall")
        self.call.update((key, value) for key, value in call.items()
                         if key not in ("args", "partialArgs", "willContinue"))
        if isinstance(call.get("args"), dict):
            self.args.update(copy.deepcopy(call["args"]))
        for item in call.get("partialArgs") or []:
            if isinstance(item, dict) and isinstance(item.get("jsonPath"), str):
                self._apply(item)

    def _apply(self, item):
        path = item["jsonPath"]
        keys = _path_keys(path)
        kind = next((key for key in _VALUE_KEYS if key in item), None)
        if keys and kind:
            target = self.args
            for key, following in zip(keys, keys[1:]):
                expected = list if isinstance(following, int) else dict
                current = _slot(target, key)
                target = current if isinstance(current, expected) else _slot(target, key, expected(), create=True)
            value = None if kind == "nullValue" else item[kind]
            if kind == "stringValue" and path in self.open_strings:
                previous = _slot(target, keys[-1])
                value = (previous if isinstance(previous, str) else "") + value
            _slot(target, keys[-1], value, create=True)
        if kind == "stringValue" and item.get("willContinue"):
            self.open_strings.add(path)
        else:
            self.open_strings.discard(path)

    def result(self):
        return {**self.part, "functionCall": {**self.call, "args": self.args}}


class PartialCallMerger:
    """按候选拼装一个流里的参数分片；收齐前不交出，收齐后变成普通的完整调用。"""

    def __init__(self):
        self._open = {}

    def feed(self, index, part):
        """返回要继续处理的内容块；调用还没收齐时返回 None。"""
        call = part.get("functionCall")
        if not isinstance(call, dict):
            return part
        if call.get("name"):
            if not _is_streaming(call):
                return part
            self._open[index] = _Call()
        elif index not in self._open:
            return part
        pending = self._open[index]
        pending.add(part)
        if call.get("willContinue"):
            return None
        del self._open[index]
        return pending.result()

    def flush(self, index):
        """候选结束时还没收尾的调用按已收到的参数交出。"""
        pending = self._open.pop(index, None)
        return pending.result() if pending else None

    def merge_chunk(self, value):
        """返回分片拼好后的响应块，不修改传入的对象；没有分片时原样返回。"""
        candidates = value.get("candidates") if isinstance(value, dict) else None
        if not isinstance(candidates, list) or not (self._open or any(
                _is_streaming(part.get("functionCall"))
                for candidate in candidates if isinstance(candidate, dict)
                for part in (candidate.get("content") or {}).get("parts") or [] if isinstance(part, dict))):
            return value
        merged = []
        for position, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                merged.append(candidate)
                continue
            index = candidate.get("index", position)
            content = candidate.get("content") if isinstance(candidate.get("content"), dict) else None
            parts = []
            for part in (content or {}).get("parts") or []:
                kept = self.feed(index, part) if isinstance(part, dict) else part
                if kept is not None:
                    parts.append(kept)
            tail = self.flush(index) if candidate.get("finishReason") else None
            if tail:
                parts.append(tail)
            if content is not None or parts:
                candidate = {**candidate, "content": {**(content or {"role": "model"}), "parts": parts}}
            merged.append(candidate)
        return {**value, "candidates": merged}
