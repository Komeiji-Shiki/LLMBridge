"""sanitize_anthropic_thinking_blocks 的单元测试

核心不变量：带签名的 thinking 与带 data 的 redacted_thinking 必须全部原样保留
（Anthropic 逐块校验历史思维链，剔除或修改任意一块都会触发
"thinking blocks cannot be modified"）；只剔除本服务拼装的无签名/无数据块。
"""
from converters.anthropic_openai import sanitize_anthropic_thinking_blocks


def _msg(role, content):
    return {"role": role, "content": content}


def test_signed_thinking_and_redacted_all_kept():
    """带签名 thinking + redacted 共存（summarized 响应）→ 全部原样保留"""
    content = [
        {"type": "thinking", "thinking": "摘要文本", "signature": "SIG"},
        {"type": "redacted_thinking", "data": "ENCRYPTED"},
        {"type": "text", "text": "回复"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
    ]
    msgs = [_msg("user", [{"type": "text", "text": "hi"}]), _msg("assistant", content)]
    out = sanitize_anthropic_thinking_blocks(msgs)
    assert out[1]["content"] == content


def test_traditional_thinking_kept():
    """传统模式：只有带签名的 thinking 块 → 原样保留（工具循环回传必需）"""
    msgs = [
        _msg("assistant", [
            {"type": "thinking", "thinking": "完整思考原文", "signature": "SIG"},
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        ]),
    ]
    out = sanitize_anthropic_thinking_blocks(msgs)
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["thinking", "tool_use"], types


def test_unsigned_thinking_removed():
    """无签名 thinking（本服务拼装）→ 剔除，避免上游 schema 校验 400"""
    msgs = [
        _msg("assistant", [
            {"type": "thinking", "thinking": "本服务拼装的无签名思维链"},
            {"type": "text", "text": "回复"},
        ]),
    ]
    out = sanitize_anthropic_thinking_blocks(msgs)
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["text"], types


def test_redacted_without_data_removed():
    """无 data 的 redacted_thinking → 剔除（原有行为）"""
    msgs = [
        _msg("assistant", [
            {"type": "redacted_thinking"},
            {"type": "text", "text": "回复"},
        ]),
    ]
    out = sanitize_anthropic_thinking_blocks(msgs)
    types = [b["type"] for b in out[0]["content"]]
    assert types == ["text"], types


def test_empty_message_dropped():
    """content 清洗后为空 → 整条消息移除"""
    msgs = [
        _msg("assistant", [{"type": "thinking", "thinking": "无签名"}]),
        _msg("user", [{"type": "text", "text": "hi"}]),
    ]
    out = sanitize_anthropic_thinking_blocks(msgs)
    assert len(out) == 1 and out[0]["role"] == "user"


def test_string_content_passthrough():
    """字符串 content 与非法输入原样返回"""
    msgs = [_msg("user", "纯文本")]
    assert sanitize_anthropic_thinking_blocks(msgs) == msgs
    assert sanitize_anthropic_thinking_blocks(None) is None


if __name__ == "__main__":
    test_signed_thinking_and_redacted_all_kept()
    test_traditional_thinking_kept()
    test_unsigned_thinking_removed()
    test_redacted_without_data_removed()
    test_empty_message_dropped()
    test_string_content_passthrough()
    print("OK: sanitize thinking blocks 全部测试通过")
