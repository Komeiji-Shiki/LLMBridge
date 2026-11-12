"""剥离 Interactions API 总结小模型泄露的原生思维链。

部分 Gemini 4（Vertex Interactions）端点在生成 thought summary 时，上游
总结小模型会把自己的原生思考（`Thinking Process:\n\n1. **Analyze the Request:** ...`）
和总结正文（`**Header**\n\nSummary`）混在一起返回。本模块提供：
- 精准识别（仅命中 summarizer 提示词指纹的片段才剥离，绝不误伤正常 raw thoughts）
- 单段/多段文本剥离与 Interaction thought step 剥离
- 逐 step 流式状态机 `ThoughtSummaryStreamStripper`（正常摘要零延迟直通，泄露思考静默缓冲至摘要出现或 step 结束）
"""
import copy
import json
import re
from typing import Callable, List, Optional, Tuple

_TP_MARKER = "Thinking Process:"
_TP_LINE_RE = re.compile(r'(?:^|\n)[ \t]*Thinking Process:')

# 总结小模型系统提示词指纹（规范化空白并去掉 ** 后小写匹配）。
# 命中任意一条即可确认当前 Thinking Process 来自 summarizer 泄露，而非主模型原生思考。
_SUMMARIZER_FINGERPRINTS = (
    "summarize the provided thoughts",
    "summarize the thoughts",
    "summarize the text",
    "existing summary",
    "input thoughts",
    'input "thoughts"',
    "input text:",
    "source text",
    "3-7 words",
    "starts with a verb",
    "wrapped in double asterisks",
    "maximum 50 words",
    "max 50 words",
    'first person ("i")',
    'first-person ("i"',
    "never repeat a header",
    "no repeating previous headers",
    "address an expert",
    "1. analyze the request:",
)

_MAX_DECIDE_CHARS = 1600
_LIST_PREFIX_RE = re.compile(r'^\s*(?:[*\-]|\d+\.)\s+$')
_BODY_LIST_RE = re.compile(r'^(?:\d+\.|[*\-])\s+')
_HEADER_CANDIDATE_RE = re.compile(r'\*\*([^*\n]{3,140}?)\*\*[ \t]*\n\n')


def _normalize_probe(text: str) -> str:
    return re.sub(r'\s+', ' ', text[:1400].lower()).replace('**', '')


def _has_summarizer_fingerprint(text: str) -> bool:
    norm = _normalize_probe(text)
    if any(fp in norm for fp in _SUMMARIZER_FINGERPRINTS):
        return True
    # 极端短截断场景：开头刚写了 Thinking Process:\n\n1. Analyze the Request: Goal/Task...
    if "analyze the request:" in norm and any(
        k in norm for k in ("goal:", "task:", "perspective:", "format", "header")
    ):
        return True
    return False


def is_summarizer_leaked_thought(text: str) -> bool:
    """判断一段文本是否以总结小模型泄露的 Thinking Process 开头。"""
    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if not stripped.startswith(_TP_MARKER):
        return False
    return _has_summarizer_fingerprint(stripped[len(_TP_MARKER):])


def _extract_summary_from_segment(seg: str) -> str:
    """从单个 `Thinking Process:` 之后的片段提取最终 `**Header**\\n\\nSummary`。

    总结小模型有时会在 Final Polish / Output Generation 里先写一份带 4 空格缩进的
    草稿，再紧接着吐出正式正文（有时中间无换行直接粘连）；有时只在 Final Polish 里
    写一份；有时结尾重复一次孤立标题；有时在正式输出前被截断。倒序遍历候选标题可
    统一覆盖上述所有形态。
    """
    headers: List[Tuple[int, int, str]] = []
    for match in _HEADER_CANDIDATE_RE.finditer(seg):
        title = match.group(1).strip()
        if not title or title.endswith(':') or title.endswith('.'):
            continue
        line_start = seg.rfind('\n', 0, match.start()) + 1
        line_prefix = seg[line_start:match.start()]
        # 跳过无序/有序列表项里的候选标题草稿（例如 `*   **Header**`）
        if _LIST_PREFIX_RE.match(line_prefix):
            continue
        # 跳过反引号包裹的样例（例如 `` `**Header**` ``）
        if line_prefix.endswith('`') and seg[match.end(1) + 2:match.end(1) + 3] == '`':
            continue
        headers.append((match.start(), match.end(), title))

    if not headers:
        return ""

    for idx in range(len(headers) - 1, -1, -1):
        _, h_end, title = headers[idx]
        next_start = headers[idx + 1][0] if idx + 1 < len(headers) else len(seg)
        body = seg[h_end:next_start].strip()
        if not body:
            continue
        first_line = body.splitlines()[0].strip()
        if _BODY_LIST_RE.match(first_line):
            continue
        body_lines = [line.strip() for line in body.splitlines() if line.strip()]
        if not body_lines:
            continue
        return f"**{title}**\n\n" + "\n".join(body_lines)

    return ""


def _split_tp_sections(text: str) -> List[Tuple[bool, str]]:
    """把文本按行首 `Thinking Process:` 切分为 `[(is_tp_marker, content), ...]`。"""
    markers = [m for m in _TP_LINE_RE.finditer(text)]
    if not markers:
        return [(False, text)]
    sections: List[Tuple[bool, str]] = []
    pos = 0
    for idx, match in enumerate(markers):
        # match.start() 可能指向前导 `\n`
        marker_start = text.find(_TP_MARKER, match.start(), match.end())
        if marker_start > pos:
            prefix = text[pos:marker_start]
            if prefix:
                sections.append((False, prefix))
        seg_start = match.end()
        seg_end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
        # 如果下一个 match.start() 在换行上，保留该段正文不含下段前导换行即可
        sections.append((True, text[seg_start:seg_end]))
        pos = seg_end
    return sections


def strip_summarizer_leaked_thoughts(text: str) -> str:
    """剥离 thought summary 中混入的 summarizer 原生思维链。

    - 未检测到 summarizer 泄露时原样返回 `text`。
    - 检测到泄露时仅保留提取出的 `**Header**\\n\\nSummary`（多段用 `\\n\\n` 分隔，
      并在非空结果末尾保留 `\\n\\n` 与正常 Interactions summary 尾随格式一致）。
    - 纯截断未产出摘要时返回 `""`。
    """
    if not isinstance(text, str) or _TP_MARKER not in text:
        return text

    sections = _split_tp_sections(text)
    if not any(is_tp and _has_summarizer_fingerprint(seg) for is_tp, seg in sections):
        return text

    trailing_newlines = text.endswith("\n\n") or text.endswith("\n")
    kept: List[str] = []
    for is_tp, seg in sections:
        if not is_tp:
            cleaned = seg.strip()
            if cleaned:
                kept.append(cleaned)
            continue
        if _has_summarizer_fingerprint(seg):
            summary = _extract_summary_from_segment(seg)
            if summary:
                kept.append(summary)
        else:
            # 极罕见情况：同一步内混有非 summarizer 的 Thinking Process，原样保留
            restored = (_TP_MARKER + seg).strip()
            if restored:
                kept.append(restored)

    if not kept:
        return ""
    return "\n\n".join(kept) + "\n\n"


def strip_thought_step(step: dict) -> dict:
    """返回剥离泄露思考后的 thought step 浅拷贝（保留 signature 等元数据）。"""
    if not isinstance(step, dict) or step.get("type") != "thought":
        return step
    summary = step.get("summary")
    if not summary:
        return step

    blocks = summary if isinstance(summary, list) else [summary]
    changed = False
    new_blocks: List[dict] = []
    # 如果一个 thought step 里有多个相邻 text block，合并后再做跨块剥离以防分片切断特征
    text_buf: List[str] = []

    def flush_text_buf() -> None:
        nonlocal changed
        if not text_buf:
            return
        raw = "".join(text_buf)
        text_buf.clear()
        stripped = strip_summarizer_leaked_thoughts(raw)
        if stripped != raw:
            changed = True
        if stripped:
            new_blocks.append({"type": "text", "text": stripped})

    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text_buf.append(str(block.get("text", "")))
        else:
            flush_text_buf()
            if isinstance(block, dict):
                new_blocks.append(block)
    flush_text_buf()

    if not changed:
        return step
    updated = dict(step)
    if isinstance(summary, list):
        updated["summary"] = new_blocks
    else:
        updated["summary"] = new_blocks[0] if new_blocks else []
    return updated


class ThoughtSummaryStreamStripper:
    """流式 thought step 摘要剥离状态机。

    生命周期按 `thought` step 隔离：
    1. 首个文本非 `Thinking Process:` 前缀 → 立即 `PASSTHROUGH`，零延迟直通。
    2. 匹配 `Thinking Process:` 前缀 → 进入 `BUFFERING` 静默收集：
       - 若 1600 字符仍未命中 summarizer 指纹 → 降级为 `PASSTHROUGH` 并补发缓冲；
       - 若缓冲中出现下一段行首 `Thinking Process:` 且前段已命中指纹 → 提前结算输出前段摘要；
       - `finish_thought()` 时对命中指纹的缓冲执行剥离并一次性下发干净摘要。
    """

    _IDLE = "idle"
    _DECIDING = "deciding"
    _PASSTHROUGH = "passthrough"
    _BUFFERING = "buffering"

    def __init__(self) -> None:
        self.state = self._IDLE
        self._buf = ""
        self._confirmed = False

    @property
    def active(self) -> bool:
        return self.state != self._IDLE

    def start_thought(self, initial_text: str = "") -> str:
        """开始新的 thought step；调用方若未显式关闭上一步，需先自行 finish_thought()。"""
        self.state = self._DECIDING
        self._buf = ""
        self._confirmed = False
        if initial_text:
            return self.feed_thought(initial_text)
        return ""

    def feed_thought(self, delta_text: str) -> str:
        """喂入 thought_summary 增量文本，返回当前可立即下发给客户端的文本。"""
        if not delta_text:
            return ""
        if self.state == self._IDLE:
            self.start_thought()
        if self.state == self._PASSTHROUGH:
            return delta_text

        self._buf += delta_text

        if self.state == self._DECIDING:
            probe = self._buf.lstrip()
            if not probe:
                return ""
            if _TP_MARKER.startswith(probe):
                # 仍可能是 `Thinking Process:` 的前缀，继续等几个字符
                return ""
            if probe.startswith(_TP_MARKER):
                self.state = self._BUFFERING
                if _has_summarizer_fingerprint(probe[len(_TP_MARKER):]):
                    self._confirmed = True
                return ""
            # 确认不是 `Thinking Process:` 开头：立即切直通并释放已攒字符
            out = self._buf
            self._buf = ""
            self.state = self._PASSTHROUGH
            return out

        # self.state == self._BUFFERING
        if not self._confirmed:
            probe = self._buf.lstrip()
            if _has_summarizer_fingerprint(probe[len(_TP_MARKER):]):
                self._confirmed = True
            elif len(probe) >= _MAX_DECIDE_CHARS:
                # 超过长度阈值仍无 summarizer 指纹，判断为普通模型原生思考，退回直通
                out = self._buf
                self._buf = ""
                self.state = self._PASSTHROUGH
                return out

        if self._confirmed:
            # 若同一 step 内出现新的行首 `Thinking Process:`，先结算输出前面已完成段落
            markers = list(_TP_LINE_RE.finditer(self._buf))
            if len(markers) >= 2:
                cut = markers[-1].start()
                head_chunk = self._buf[:cut]
                # 保留末尾分隔处的换行符给下一段起始匹配
                if self._buf[cut:cut + 1] == "\n":
                    self._buf = self._buf[cut + 1:]
                else:
                    self._buf = self._buf[cut:]
                cleaned = strip_summarizer_leaked_thoughts(head_chunk)
                if cleaned and not cleaned.endswith("\n"):
                    cleaned += "\n\n"
                return cleaned
        return ""

    def finish_thought(self) -> str:
        """当前 thought step 结束，返回需要补发的全部干净摘要。"""
        if self.state == self._IDLE:
            return ""
        state = self.state
        buf = self._buf
        confirmed = self._confirmed
        self.state = self._IDLE
        self._buf = ""
        self._confirmed = False

        if not buf or state == self._PASSTHROUGH:
            return ""
        if state == self._DECIDING:
            return buf
        # _BUFFERING
        if confirmed or is_summarizer_leaked_thought(buf):
            return strip_summarizer_leaked_thoughts(buf)
        return buf


def strip_interactions_response_thoughts(response: dict) -> dict:
    """剥离 Interactions 非流式响应或 interaction 对象中 thought step 的泄露思考。"""
    if not isinstance(response, dict):
        return response
    steps = response.get("steps")
    if not isinstance(steps, list):
        return response
    changed = False
    new_steps = []
    for step in steps:
        cleaned = strip_thought_step(step)
        if cleaned is not step:
            changed = True
        new_steps.append(cleaned)
    if not changed:
        return response
    updated = dict(response)
    updated["steps"] = new_steps
    return updated


def _sse_event_bytes(event: dict) -> bytes:
    event_type = event.get("event_type")
    payload = json.dumps(event, ensure_ascii=False)
    if isinstance(event_type, str) and event_type:
        return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")
    return f"data: {payload}\n\n".encode("utf-8")


class InteractionsNativeStreamStripper:
    """Interactions 原生 SSE 事件流中的 thought step 思考剥离器。

    - 未命中泄露时原样保留原始事件（透传分支可零拷贝转发原 bytes）。
    - 命中泄露时静默吞掉 summarizer 原生思考 delta，在结算点以标准
      `step.delta(thought_summary)` 发出干净摘要，并完整保留 signature 等字段。
    """

    def __init__(self) -> None:
        self._stripper = ThoughtSummaryStreamStripper()
        self._step_index: Optional[int] = None
        self._passthrough_direct = False

    def _emit_summary_event(self, text: str, index: Optional[int] = None) -> dict:
        event: dict = {
            "event_type": "step.delta",
            "delta": {
                "type": "thought_summary",
                "content": {"type": "text", "text": text},
            },
        }
        idx = index if index is not None else self._step_index
        if idx is not None:
            event["index"] = idx
        return event

    def _flush_events(self) -> List[dict]:
        if not self._stripper.active:
            return []
        flushed = self._stripper.finish_thought()
        idx = self._step_index
        self._step_index = None
        self._passthrough_direct = False
        return [self._emit_summary_event(flushed, idx)] if flushed else []

    def process_event(self, event: dict) -> Tuple[bool, List[dict]]:
        """处理单个解码后的 Interactions SSE 事件。

        Returns:
            (passthrough_raw, emitted_events):
            - `passthrough_raw=True` 表示事件内容未改写，调用方可原样转发原始 SSE bytes。
            - `passthrough_raw=False` 时调用方应依次序列化发送 `emitted_events`（可能为空）。
        """
        if not isinstance(event, dict):
            return True, [event]
        event_type = event.get("event_type")

        if event_type == "step.start":
            flushed = self._flush_events()
            step = event.get("step")
            if isinstance(step, dict) and step.get("type") == "thought":
                self._step_index = event.get("index")
                summary = step.get("summary")
                blocks = summary if isinstance(summary, list) else ([summary] if summary else [])
                texts = [str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
                initial = "".join(texts)
                out = self._stripper.start_thought(initial)
                if not initial and not flushed:
                    return True, [event]
                if self._stripper.state == ThoughtSummaryStreamStripper._PASSTHROUGH and out == initial and not flushed:
                    self._passthrough_direct = True
                    return True, [event]
                new_step = dict(step)
                other_blocks = [b for b in blocks if isinstance(b, dict) and b.get("type") != "text"]
                if out:
                    other_blocks.insert(0, {"type": "text", "text": out})
                if other_blocks:
                    new_step["summary"] = other_blocks if isinstance(summary, list) else other_blocks[0]
                else:
                    new_step.pop("summary", None)
                return False, [*flushed, {**event, "step": new_step}]
            return (not flushed), [*flushed, event]

        if event_type == "step.delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") in ("thought_summary", "thought"):
                if event.get("index") is not None:
                    self._step_index = event["index"]
                block = delta.get("content")
                if isinstance(block, dict) and block.get("type") != "text":
                    return True, [event]
                text = (block.get("text", "") if isinstance(block, dict) else delta.get("text", "")) or ""
                if not self._stripper.active:
                    self._stripper.start_thought()
                out = self._stripper.feed_thought(str(text))
                if self._stripper.state == ThoughtSummaryStreamStripper._PASSTHROUGH and out == text:
                    self._passthrough_direct = True
                    return True, [event]
                if not out:
                    return False, []
                # 如果从 DECIDING 转为 PASSTHROUGH 并一次性吐出缓冲，构造对应增量
                return False, [self._emit_summary_event(out, event.get("index"))]
            if isinstance(delta, dict) and delta.get("type") == "thought_signature":
                return True, [event]
            flushed = self._flush_events()
            return (not flushed), [*flushed, event]

        if event_type == "step.stop":
            flushed = self._flush_events()
            if not flushed:
                return True, [event]
            return False, [*flushed, event]

        if event_type == "interaction.completed":
            flushed = self._flush_events()
            interaction = event.get("interaction")
            cleaned = strip_interactions_response_thoughts(interaction) if isinstance(interaction, dict) else interaction
            if cleaned is interaction and not flushed:
                return True, [event]
            out_event = {**event, "interaction": cleaned} if cleaned is not interaction else event
            return False, [*flushed, out_event]

        return True, [event]

    def process_chunk(self, chunk: bytes) -> Tuple[List[dict], bytes]:
        """处理来自 `call_api_passthrough` 的单个 SSE 事件 bytes 块。

        Returns:
            (observed_events, output_bytes)
        """
        from services.protocol_events import payloads
        events = payloads(chunk)
        if not events:
            return [], chunk
        all_passthrough = True
        emitted: List[dict] = []
        for ev in events:
            if isinstance(ev, dict) and ev.get("done") is True and len(ev) == 1:
                emitted.append(ev)
                continue
            pt, out_evs = self.process_event(ev)
            if not pt:
                all_passthrough = False
            emitted.extend(out_evs)
        if all_passthrough:
            return events, chunk
        out_bytes = b"".join(
            b"data: [DONE]\n\n" if (isinstance(e, dict) and e.get("done") is True and len(e) == 1)
            else _sse_event_bytes(e)
            for e in emitted
        )
        return emitted, out_bytes

