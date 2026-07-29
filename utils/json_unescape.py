"""JSON \\uXXXX 转义的流式解码工具。

模型在长上下文下经常以 ASCII 转义风格生成工具调用参数（中文变成 \\uXXXX），
这在 JSON 语义上完全合法，但客户端流式预览时直接显示原始 JSON 文本，
中文全部变成 \\u5468 之类的转义，完全不可读。本模块在服务端把安全的
\\uXXXX 转义提前解码为 UTF-8 明文再发给下游，并保证 JSON 语义不变：

- 通过逐字符状态机只解码真正的 JSON 转义；字面反斜杠后的 uXXXX 文本
  （如正则 "\\\\u4e00-\\\\u9fff"）不会被误解码
- 保留必须转义的字符：控制字符(<0x20)、双引号、反斜杠、DEL/C1 控制符
  (0x7F-0x9F)、U+2028/U+2029（JS 兼容性）
- 代理对成对解码（emoji 等增补平面字符）；孤立代理保留转义原样
- 跨 chunk 边界安全：片段尾部的不完整转义序列缓冲到下一片段
"""
import json
import logging

logger = logging.getLogger(__name__)

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _is_safe_literal(cp: int) -> bool:
    """码点是否可以安全地从 \\uXXXX 转义解码为字面字符而不破坏 JSON。"""
    if cp < 0x20:                 # JSON 规定控制字符必须转义
        return False
    if cp in (0x22, 0x5C):        # " 和 \\ 字面化会破坏 JSON 结构
        return False
    if 0x7F <= cp <= 0x9F:        # DEL 和 C1 控制符，字面化无显示意义
        return False
    if cp in (0x2028, 0x2029):    # JS 行分隔符，保留转义避免旧解析器出错
        return False
    return True


def _is_unicode_escape_prefix(rest: str) -> bool:
    """rest 是否为 "\\uHHHH" 的合法前缀（用于判断是否值得缓冲等待后续字节）。"""
    if not rest:
        return True
    if rest[0] != "\\":
        return False
    if len(rest) == 1:
        return True
    if rest[1] != "u":
        return False
    return all(c in _HEX_DIGITS for c in rest[2:])


class StreamingUnicodeUnescaper:
    """把 JSON 文本片段流中的 \\uXXXX 转义解码为明文字符（跨片段安全）。

    用法：对每个独立的 JSON 文本流（如单个 tool_use 块的参数增量）创建
    一个实例，逐片段调用 feed()，流结束时调用 flush() 取出缓冲残留。
    正常完整的 JSON 不会产生残留；只有流在转义序列中间被截断时才有。
    """

    __slots__ = ("_tail",)

    def __init__(self):
        self._tail = ""

    @property
    def pending(self) -> bool:
        """是否有缓冲中的不完整转义序列尾部。"""
        return bool(self._tail)

    def feed(self, fragment: str) -> str:
        """解码一个片段，返回可安全输出的部分（尾部不完整序列被缓冲）。"""
        if not fragment:
            return ""
        s = self._tail + fragment
        self._tail = ""
        if "\\" not in s:
            return s

        out = []
        i = 0
        n = len(s)
        while i < n:
            if s[i] != "\\":
                # 快进到下一个反斜杠
                j = s.find("\\", i)
                if j < 0:
                    out.append(s[i:])
                    break
                out.append(s[i:j])
                i = j
                continue

            remaining = n - i
            if remaining == 1:
                # 片段末尾的孤立反斜杠，等待下一片段
                self._tail = s[i:]
                break

            nxt = s[i + 1]
            if nxt != "u":
                # \\ \" \n 等普通转义：原样输出两个字符（消费掉转义状态）
                out.append(s[i:i + 2])
                i += 2
                continue

            if remaining < 6:
                # \uXX... 不完整：若已有部分是合法 hex 则缓冲等待，否则原样输出
                if all(c in _HEX_DIGITS for c in s[i + 2:]):
                    self._tail = s[i:]
                else:
                    out.append(s[i:])
                break

            hex4 = s[i + 2:i + 6]
            if not all(c in _HEX_DIGITS for c in hex4):
                # 非法 \u 转义（JSON 本身已损坏），原样保留现场
                out.append(s[i:i + 2])
                i += 2
                continue

            cp = int(hex4, 16)
            if not _is_safe_literal(cp):
                out.append(s[i:i + 6])
                i += 6
                continue

            if 0xD800 <= cp <= 0xDBFF:
                # 高位代理：必须与紧随的低位代理转义成对解码
                if n < i + 12:
                    rest = s[i + 6:]
                    if _is_unicode_escape_prefix(rest):
                        # 后续可能是低位代理转义，缓冲整段等待
                        self._tail = s[i:]
                        break
                    # 后面不是转义，孤立高位代理，保留原样
                    out.append(s[i:i + 6])
                    i += 6
                    continue
                if s[i + 6] == "\\" and s[i + 7] == "u":
                    lo_hex = s[i + 8:i + 12]
                    if all(c in _HEX_DIGITS for c in lo_hex):
                        lo = int(lo_hex, 16)
                        if 0xDC00 <= lo <= 0xDFFF:
                            out.append(chr(0x10000 + ((cp - 0xD800) << 10) + (lo - 0xDC00)))
                            i += 12
                            continue
                # 无效配对，孤立高位代理保留原样
                out.append(s[i:i + 6])
                i += 6
                continue

            if 0xDC00 <= cp <= 0xDFFF:
                # 孤立低位代理，保留原样
                out.append(s[i:i + 6])
                i += 6
                continue

            out.append(chr(cp))
            i += 6

        return "".join(out)

    def flush(self) -> str:
        """流结束时取出缓冲的不完整尾部（原样返回，保证字节不丢）。"""
        tail = self._tail
        self._tail = ""
        return tail


def unescape_json_unicode(text: str) -> str:
    """一次性解码完整 JSON 文本中的 \\uXXXX 转义（不要求 JSON 合法）。"""
    unescaper = StreamingUnicodeUnescaper()
    return unescaper.feed(text) + unescaper.flush()


def normalize_tool_args_json(args_str: str) -> str:
    """规范化完整的工具调用参数 JSON 字符串。

    合法 JSON 直接重新序列化为中文明文（顺带规范空白）；非法 JSON
    （流截断等）退化为字符级解码，转义仍被还原且原有字节语义不变。
    """
    if not args_str or "\\u" not in args_str:
        return args_str
    try:
        return json.dumps(json.loads(args_str), ensure_ascii=False)
    except Exception:
        return unescape_json_unicode(args_str)


def normalize_response_tool_args(response_json: dict) -> bool:
    """规范化 OpenAI 非流式响应中所有 tool_calls 的 arguments，返回是否有修改。"""
    if not isinstance(response_json, dict):
        return False
    modified = False
    for choice in response_json.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        for tc in message.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            func = tc.get("function")
            if not isinstance(func, dict):
                continue
            args = func.get("arguments")
            if isinstance(args, str) and args:
                normalized = normalize_tool_args_json(args)
                if normalized != args:
                    func["arguments"] = normalized
                    modified = True
    return modified


class AnthropicSSEToolArgsRewriter:
    """流式重写 Anthropic SSE 字节流：input_json_delta 的 \\uXXXX 提前解码。

    用于 /v1/messages 直通链路（上游 Anthropic → 下游 Anthropic 客户端），
    原先是纯字节透传，工具参数增量中的转义原样直达客户端。

    - 按事件块（\\n\\n 分隔）缓冲，保证事件的 event:/data: 行成对处理
    - 只重写 input_json_delta 事件的 partial_json，其余字节原样转发
    - 每个 content block index 维护独立的解码器状态
    - content_block_stop 时冲刷残留：不完整转义原样补发为一个附加 delta
    """

    __slots__ = ("_buf", "_unescapers")

    def __init__(self):
        # 🔧 性能：bytearray 缓冲。bytes 的 += 每次全量复制（CPython 仅对 str
        # 有原地优化），长流式响应下逐 chunk 退化为 O(n²)
        self._buf = bytearray()
        self._unescapers = {}

    def feed(self, chunk: bytes) -> bytes:
        """处理一段字节，返回可转发的完整事件块（可能为空，等待更多字节）。"""
        self._buf += chunk
        out = bytearray()
        while True:
            # 🔧 同时匹配 LF (\n\n) 和 CRLF (\r\n\r\n) 分隔符
            sep_lf = self._buf.find(b"\n\n")
            sep_crlf = self._buf.find(b"\r\n\r\n")
            if sep_lf < 0 and sep_crlf < 0:
                break
            # 选择先出现的分隔符（CRLF 优先当同时出现时，因为它更具体）
            if sep_lf < 0:
                sep = sep_crlf
                sep_len = 4
            elif sep_crlf < 0:
                sep = sep_lf
                sep_len = 2
            else:
                # 两者同时存在，取先出现的
                if sep_crlf <= sep_lf:
                    sep = sep_crlf
                    sep_len = 4
                else:
                    sep = sep_lf
                    sep_len = 2
            block = bytes(self._buf[:sep + sep_len])
            del self._buf[:sep + sep_len]  # 原地消费头部，不产生新对象
            out += self._process_block(block)
        return bytes(out)

    def flush(self) -> bytes:
        """流结束时取出缓冲的未完成事件块（原样返回）。"""
        rest = bytes(self._buf)
        self._buf = bytearray()
        self._unescapers.clear()
        return rest

    def _process_block(self, block: bytes) -> bytes:
        # 快速过滤：与 content block 无关的事件原样转发
        if b"content_block" not in block and b"message_stop" not in block:
            return block

        lines = block.split(b"\n")
        data_idx = None
        for i, ln in enumerate(lines):
            if ln.startswith(b"data:"):
                if data_idx is not None:
                    return block  # 多条 data 行的非常规事件，保守转发
                data_idx = i
        if data_idx is None:
            return block

        try:
            event = json.loads(lines[data_idx][5:].strip())
        except Exception:
            return block
        if not isinstance(event, dict):
            return block

        etype = event.get("type")
        if etype == "content_block_start":
            cb = event.get("content_block")
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                self._unescapers[event.get("index", 0)] = StreamingUnicodeUnescaper()
            return block

        if etype == "content_block_delta":
            delta = event.get("delta")
            if not (isinstance(delta, dict) and delta.get("type") == "input_json_delta"):
                return block
            idx = event.get("index", 0)
            unescaper = self._unescapers.get(idx)
            if unescaper is None:
                # 未收到 start（接管半截流），按需补建
                unescaper = self._unescapers[idx] = StreamingUnicodeUnescaper()
            pj = delta.get("partial_json", "")
            if not isinstance(pj, str):
                return block
            if "\\" not in pj and not unescaper.pending:
                return block  # 快速路径：解码结果必然与原文相同
            decoded = unescaper.feed(pj)
            if decoded == pj:
                return block
            delta["partial_json"] = decoded
            lines[data_idx] = b"data: " + json.dumps(
                event, ensure_ascii=False).encode("utf-8")
            return b"\n".join(lines)

        if etype == "content_block_stop":
            idx = event.get("index", 0)
            unescaper = self._unescapers.pop(idx, None)
            if unescaper is not None:
                tail = unescaper.flush()
                if tail:
                    # 残留只在流于转义中间截断时出现；补发保证字节完整
                    filler = {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {"type": "input_json_delta", "partial_json": tail},
                    }
                    return (
                        b"event: content_block_delta\ndata: "
                        + json.dumps(filler, ensure_ascii=False).encode("utf-8")
                        + b"\n\n" + block
                    )
            return block

        if etype == "message_stop":
            self._unescapers.clear()
        return block
