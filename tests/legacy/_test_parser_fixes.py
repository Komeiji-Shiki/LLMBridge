"""验证本轮修复的解析行为：

1. extract_partial_content 对转义引号截断内容的提取（旧版丢失）
2. extract_finish_info 对嵌套 usage 对象的解析（旧版正则截断失败）
3. extract_image_urls 嵌套结构 + 截断保留
4. check_cloudflare 正文误判守卫（流数据标记存在时不误杀）
5. is_control_marker 行首匹配（正文含 "a3:" 不误判）
6. 文本/思维链提取与旧版等价（含转义、剩余 buffer）
7. iter_openai_sse_payloads：CRLF 分隔 + 多字节字符跨 chunk 切断
"""
import asyncio
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

from services.stream_parsers import (
    StreamPatternMatcher,
    extract_partial_content,
    is_control_marker,
)

m = StreamPatternMatcher()

# 1. 转义引号的截断内容
text, rest = extract_partial_content('a0:"he said \\"hi\\" and')
assert text == 'he said "hi" and', f"转义截断提取失败: {text!r}"
text2, _ = extract_partial_content('a0:"简单文本')
assert text2 == "简单文本", f"普通截断提取失败: {text2!r}"
print("1. 转义引号截断提取: OK")

# 2. 嵌套 usage 的 finish 数据
buf = 'ad:{"finishReason":"stop","usage":{"promptTokens":10,"completionTokens":20}}\n'
finish, rest = m.extract_finish_info(buf)
assert finish is not None and finish["finishReason"] == "stop", f"嵌套finish解析失败: {finish}"
assert finish["usage"]["promptTokens"] == 10
# 截断的 finish 数据应保留 buffer 等待补全
finish2, rest2 = m.extract_finish_info('ad:{"finishReason":"stop","usage":{"promp')
assert finish2 is None and rest2 == 'ad:{"finishReason":"stop","usage":{"promp'
print("2. 嵌套/截断 finish 解析: OK")

# 3. 图片提取：嵌套结构 + 截断保留
img_buf = 'a2:[{"type":"image","image":"https://x/1.png","extra":{"a":[1,2]}}]\n'
imgs, rest = m.extract_image_urls(img_buf)
assert len(imgs) == 1 and imgs[0]["image"] == "https://x/1.png", f"图片解析失败: {imgs}"
imgs2, rest2 = m.extract_image_urls('a2:[{"type":"image","image":"https://x/2.pn')
assert imgs2 == [] and rest2.startswith('a2:'), "截断图片应保留buffer"
print("3. 图片嵌套/截断解析: OK")

# 4. CF 守卫：正文里出现 CF 特征文本不误杀
normal_stream = 'a0:"To fix this, Enable JavaScript and cookies to continue browsing"\n'
assert not m.check_cloudflare(normal_stream), "正常流被误判为CF"
cf_page = '<html><head><title>Just a moment...</title></head><body>...</body></html>'
assert m.check_cloudflare(cf_page), "真CF页面未检出"
print("4. Cloudflare 误判守卫: OK")

# 5. 控制标记行首匹配
assert not is_control_marker('some text mentioning a3: in prose'), "正文含a3:被误判"
assert is_control_marker('ad:{"finishReason":"stop"}'), "行首ad:未检出"
assert is_control_marker('data\nbe:[]'), "换行后be:未检出"
print("5. 控制标记行首匹配: OK")

# 6. 文本/思维链提取等价性
buf = 'ag:"思考\\n片段"\na0:"正文\\"引号\\""\na0:"第二段"\nresidual'
reasoning, buf1 = m.extract_reasoning_content(buf)
assert reasoning == ["思考\n片段"], f"思维链: {reasoning}"
contents, buf2 = m.extract_text_content(buf1)
assert contents == ['正文"引号"', "第二段"], f"文本: {contents}"
assert buf2 == "\nresidual", f"剩余buffer: {buf2!r}"
print("6. 文本/思维链提取等价: OK")

# 7. iter_openai_sse_payloads: CRLF + 跨 chunk 多字节
from converters.anthropic_openai import iter_openai_sse_payloads


class _FakeResp:
    def __init__(self, chunks):
        async def _gen():
            for c in chunks:
                yield c
        self.body_iterator = _gen()


async def _collect(chunks):
    out = []
    async for p in iter_openai_sse_payloads(_FakeResp(chunks)):
        out.append(p)
    return out

# CRLF 分隔的事件
payloads = asyncio.run(_collect([b'data: {"a":1}\r\n\r\ndata: {"b":2}\r\n\r\n']))
assert payloads == ['{"a":1}', '{"b":2}'], f"CRLF分隔失败: {payloads}"

# 中文字符切在 chunk 边界（"中" = e4 b8 ad）
raw = 'data: {"t":"中文"}\n\n'.encode("utf-8")
split_at = raw.index("中".encode("utf-8")) + 1  # 切在多字节序列中间
payloads2 = asyncio.run(_collect([raw[:split_at], raw[split_at:]]))
assert payloads2 == ['{"t":"中文"}'], f"跨chunk多字节失败: {payloads2}"
obj = json.loads(payloads2[0])
assert obj["t"] == "中文"
print("7. SSE CRLF/跨chunk多字节: OK")

print("\n全部通过 ✓")
