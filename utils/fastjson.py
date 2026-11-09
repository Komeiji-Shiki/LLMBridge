"""整份请求体、日志这类大 JSON 的序列化。

标准库要先把整份 JSON 放进一个 str：正文里只要有一个 emoji，CPython 就按每字符
4 字节存，12MB 的请求体多占 40MB 以上，序列化也慢一个量级。orjson 直接写出
UTF-8 字节。它不接受的输入（非字符串键、超出 64 位的整数）退回标准库。

解析仍用标准库：orjson 会把超出 64 位的整数读成浮点数，工具参数里的大整数会丢精度。
"""
import json

import orjson


def dumps(value) -> bytes:
    """紧凑 UTF-8 字节，等价于 json.dumps(ensure_ascii=False, separators=(',', ':')).encode()。

    只有 NaN 和无穷大不同：标准库写成不合法的 NaN/Infinity，这里写成 null。
    """
    try:
        return orjson.dumps(value)
    except TypeError:
        return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
