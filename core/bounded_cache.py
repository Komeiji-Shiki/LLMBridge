"""带字节上限的 TTL 缓存。

cachetools 的 TTLCache 只限制条数，不限制内容大小。图片缓存存的是
base64 字符串，一条可能是几百 KB 到几 MB，仅靠 500 条的上限无法约束
内存。这里在 TTLCache 之上增加总字节预算，超预算时按 LRU 顺序驱逐。
"""

from __future__ import annotations

import sys
from typing import Any

from cachetools import TTLCache


def estimate_value_size(value: Any) -> int:
    """估算缓存值占用的字节数（对字符串/字节串精确，其他类型用 sys.getsizeof）。"""
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return len(value)
    if isinstance(value, dict):
        return sum(estimate_value_size(k) + estimate_value_size(v) for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return sum(estimate_value_size(item) for item in value)
    return sys.getsizeof(value)


class ByteLimitedTTLCache(TTLCache):
    """TTLCache + 总字节预算。

    单个值超过总预算时不缓存（避免一条超大图片独占全部预算）。
    """

    def __init__(self, maxsize: int, ttl: float, max_bytes: int, *args, **kwargs):
        super().__init__(maxsize=maxsize, ttl=ttl, *args, **kwargs)
        try:
            parsed_bytes = int(max_bytes)
        except (TypeError, ValueError):
            parsed_bytes = 64 * 1024 * 1024
        self.max_bytes = max(0, parsed_bytes)
        self._sizes: dict = {}
        self._total_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def _forget(self, key: Any) -> None:
        self._total_bytes -= self._sizes.pop(key, 0)
        if self._total_bytes < 0:
            self._total_bytes = 0

    def __setitem__(self, key: Any, value: Any) -> None:
        size = estimate_value_size(value)
        if size > self.max_bytes:
            # 单条超预算：清掉同键旧值，但不缓存新值
            if key in self:
                self.pop(key, None)
            return

        if key in self._sizes:
            self._total_bytes -= self._sizes.pop(key)
        super().__setitem__(key, value)
        self._sizes[key] = size
        self._total_bytes += size

        while self._total_bytes > self.max_bytes and len(self) > 0:
            try:
                self.popitem()
            except KeyError:
                break

    def __delitem__(self, key: Any) -> None:
        try:
            super().__delitem__(key)
        finally:
            # 过期条目在 TTLCache 中会“删除后抛 KeyError”，这里仍然要同步计数
            self._forget(key)

    def popitem(self):
        key, value = super().popitem()
        self._forget(key)
        return key, value

    def expire(self, time=None):
        # cachetools 的 expire() 直接调用 Cache.__delitem__，绕过本类的
        # __delitem__，因此这里需要自己同步字节计数。
        expired = super().expire(time)
        if expired:
            for key, _ in expired:
                self._forget(key)
        return expired

    def clear(self) -> None:
        super().clear()
        self._sizes.clear()
        self._total_bytes = 0
