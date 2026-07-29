"""JSONC 文本的定点编辑与配置文件的原子写入。

为什么需要这个模块：
- config.jsonc 里有大量说明性注释，是这个项目最主要的"文档"。任何
  "解析成 dict → json.dump 覆盖回去"的保存路径都会把注释全部抹掉
  （管理面板保存一次 tokenizer 映射，整份配置注释就没了）。
  set_jsonc_value 直接在原始文本上替换目标 key 的值，其余字节原样保留。
- model_endpoint_map.json 已有 160KB+，直接 open(...,'w') 覆盖写在
  进程被强杀 / 磁盘满时会留下半截文件，等于整套模型配置丢失。
  atomic_write_text 用临时文件 + os.replace 保证要么旧内容要么新内容。
"""
import json
import os
import threading
from typing import Any, Optional

_WHITESPACE = " \t\r\n"


def _skip_ws_and_comments(text: str, i: int) -> int:
    """跳过空白与 // 、/* */ 注释，返回下一个有效字符的下标。"""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _WHITESPACE:
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            if text[i + 1] == "/":
                nl = text.find("\n", i)
                i = n if nl == -1 else nl + 1
                continue
            if text[i + 1] == "*":
                end = text.find("*/", i + 2)
                i = n if end == -1 else end + 2
                continue
        break
    return i


def _scan_string(text: str, i: int) -> int:
    """i 指向起始双引号，返回结束双引号之后的下标。"""
    n = len(text)
    i += 1
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == '"':
            return i + 1
        i += 1
    return n


def _scan_value(text: str, i: int) -> int:
    """从值的起始位置扫到值的结束位置（不含），能正确跳过字符串与注释。"""
    n = len(text)
    i = _skip_ws_and_comments(text, i)
    if i >= n:
        return n

    ch = text[i]
    if ch == '"':
        return _scan_string(text, i)

    if ch in "{[":
        depth = 0
        while i < n:
            c = text[i]
            if c == '"':
                i = _scan_string(text, i)
                continue
            if c == "/" and i + 1 < n and text[i + 1] in "/*":
                i = _skip_ws_and_comments(text, i)
                continue
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n

    # 裸值（数字 / true / false / null）：扫到分隔符为止
    while i < n and text[i] not in ",}]\r\n" and not (
        text[i] == "/" and i + 1 < n and text[i + 1] in "/*"
    ):
        i += 1
    return i


def find_top_level_key(text: str, key: str) -> Optional[tuple]:
    """定位顶层对象中 "key": <value> 的值区间，返回 (值起点, 值终点)。

    只匹配深度为 1 的键，避免命中嵌套对象里的同名键。找不到返回 None。
    """
    n = len(text)
    i = _skip_ws_and_comments(text, 0)
    if i >= n or text[i] != "{":
        return None
    i += 1

    while i < n:
        i = _skip_ws_and_comments(text, i)
        if i >= n:
            break
        if text[i] == "}":
            break
        if text[i] == ",":
            i += 1
            continue
        if text[i] != '"':
            # 结构异常，放弃定位
            return None

        key_end = _scan_string(text, i)
        try:
            current_key = json.loads(text[i:key_end])
        except ValueError:
            return None

        i = _skip_ws_and_comments(text, key_end)
        if i >= n or text[i] != ":":
            return None
        value_start = _skip_ws_and_comments(text, i + 1)
        value_end = _scan_value(text, value_start)

        if current_key == key:
            return value_start, value_end

        i = value_end

    return None


def set_jsonc_value(text: str, key: str, value: Any, indent: int = 2) -> str:
    """替换 JSONC 文本中顶层 key 的值，保留文件其余部分（含注释）。

    key 不存在时追加到顶层对象末尾。value 用 json.dumps 序列化，
    嵌套对象按 indent 缩进并整体右移一级，与手写配置的观感一致。
    """
    serialized = json.dumps(value, ensure_ascii=False, indent=indent)
    if "\n" in serialized:
        pad = " " * indent
        serialized = ("\n" + pad).join(serialized.split("\n"))

    span = find_top_level_key(text, key)
    if span is not None:
        start, end = span
        return text[:start] + serialized + text[end:]

    # key 不存在：插入到顶层对象的收尾大括号之前
    close = text.rfind("}")
    if close == -1:
        raise ValueError("目标文本不是一个 JSON 对象，无法插入新键")

    head = text[:close].rstrip()
    tail = text[close:]
    # 注释感知：若 head 末尾是行注释（//），把逗号插入注释之前，避免逗号被注释吞掉
    comment_pos = head.rfind('\n')
    if comment_pos >= 0 and '//' in head[comment_pos:]:
        # 最后一行是注释，把逗号插到注释行之前
        separator = "" if head[:comment_pos].rstrip().endswith("{") else ","
        pad = " " * indent
        return f'{head[:comment_pos].rstrip()}{separator}\n{pad}"{key}": {serialized}\n{head[comment_pos:]}\n{tail}'
    separator = "" if head.endswith("{") else ","
    pad = " " * indent
    return f'{head}{separator}\n{pad}"{key}": {serialized}\n{tail}'


def set_jsonc_values(text: str, updates: dict, indent: int = 2) -> str:
    """批量替换顶层键的值，保留注释与未涉及的键。

    只更新 updates 中出现的键：文本里存在但 updates 没提到的键原样保留，
    因此表单只回传自己认得的字段也不会误删配置。
    """
    for key, value in updates.items():
        text = set_jsonc_value(text, key, value, indent=indent)
    return text


def atomic_write_text(path: str, content: str, encoding: str = "utf-8") -> None:
    """原子写文本文件：先写同目录临时文件，再 os.replace 覆盖目标。

    同目录是必需的——跨文件系统的 replace 不是原子操作。
    临时文件名包含 pid+tid 防止并发写端覆盖彼此。
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.tmp.{os.getpid()}.{threading.get_ident()}")
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, data: Any, indent: int = 2) -> None:
    """原子写 JSON 文件（供 model_endpoint_map.json 等大配置使用）。"""
    atomic_write_text(path, json.dumps(data, indent=indent, ensure_ascii=False))
