"""伪造对话历史注入逻辑的单元测试。

覆盖 inject_fake_conversation 与扩展后的 inject_system_prompt：
- 伪造整轮/多轮对话插入位置
- assistant 消息 reasoning_content（思维链）透传
- tool 角色消息 tool_call_id 透传
- content 为空仅伪造对话的场景
- convert_system_to_user 兼容模式
- 原消息列表不被原地修改
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes._direct_api_utils import (
    inject_system_prompt,
    inject_fake_conversation,
    ensure_reasoning_noop_tool,
)


class TestInjectFakeConversation(unittest.TestCase):
    def test_insert_at_head_when_no_system(self):
        msgs = [{"role": "user", "content": "hi"}]
        fake = [{"role": "assistant", "content": "fake reply", "reasoning_content": "fake thinking"}]
        out = inject_fake_conversation(msgs, fake)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["reasoning_content"], "fake thinking")
        self.assertEqual(out[1]["role"], "user")

    def test_insert_after_leading_system(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        fake = [
            {"role": "user", "content": "fake q"},
            {"role": "assistant", "content": "fake a"},
        ]
        out = inject_fake_conversation(msgs, fake)
        self.assertEqual([m["role"] for m in out], ["system", "user", "assistant", "user"])
        self.assertEqual(out[1]["content"], "fake q")
        self.assertEqual(out[2]["content"], "fake a")

    def test_tool_role_with_tool_call_id(self):
        msgs = [{"role": "user", "content": "hi"}]
        fake = [
            {"role": "assistant", "content": "", "reasoning_content": "think"},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        out = inject_fake_conversation(msgs, fake)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[0]["content"], "")
        self.assertEqual(out[1]["role"], "tool")
        self.assertEqual(out[1]["tool_call_id"], "call_1")

    def test_does_not_mutate_original(self):
        msgs = [{"role": "user", "content": "hi"}]
        fake = [{"role": "assistant", "content": "a"}]
        inject_fake_conversation(msgs, fake)
        self.assertEqual(msgs, [{"role": "user", "content": "hi"}])


class TestInjectSystemPromptWithFake(unittest.TestCase):
    def test_only_fake_conversation(self):
        cfg = {"enabled": True, "fake_conversation": [{"role": "assistant", "content": "fake"}]}
        msgs = [{"role": "user", "content": "hi"}]
        out = inject_system_prompt(msgs, cfg)
        self.assertEqual(out[0]["role"], "assistant")
        self.assertEqual(out[1]["role"], "user")

    def test_content_and_fake_conversation(self):
        cfg = {
            "enabled": True,
            "position": "before_system",
            "content": "INJECTED",
            "fake_conversation": [{"role": "assistant", "content": "fake"}],
        }
        msgs = [{"role": "system", "content": "original"}, {"role": "user", "content": "hi"}]
        out = inject_system_prompt(msgs, cfg)
        self.assertIn("INJECTED", out[0]["content"])
        self.assertEqual(out[1]["role"], "assistant")
        self.assertEqual(out[1]["content"], "fake")
        self.assertEqual(out[2]["role"], "user")

    def test_convert_system_to_user_with_fake(self):
        cfg = {
            "enabled": True,
            "content": "INJECTED",
            "fake_conversation": [{"role": "assistant", "content": "fake"}],
        }
        msgs = [{"role": "user", "content": "hi"}]
        out = inject_system_prompt(msgs, cfg, convert_system_to_user=True)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"], "INJECTED")
        self.assertEqual(out[1]["role"], "assistant")
        self.assertEqual(out[1]["content"], "Understood. I'll follow these instructions.")
        self.assertEqual(out[2]["role"], "assistant")
        self.assertEqual(out[2]["content"], "fake")
        self.assertEqual(out[3]["role"], "user")

    def test_disabled_returns_unchanged(self):
        cfg = {"enabled": False, "fake_conversation": [{"role": "assistant", "content": "fake"}]}
        msgs = [{"role": "user", "content": "hi"}]
        out = inject_system_prompt(msgs, cfg)
        self.assertEqual(out, msgs)


class TestEnsureReasoningNoopTool(unittest.TestCase):
    def test_inject_noop_when_reasoning_and_no_tools(self):
        cfg = {
            "enabled": True,
            "fake_conversation": [
                {"role": "assistant", "content": "a", "reasoning_content": "think"},
            ],
        }
        req = {"messages": [{"role": "user", "content": "hi"}]}
        out = ensure_reasoning_noop_tool(req, cfg)
        self.assertIn("tools", out)
        self.assertEqual(len(out["tools"]), 1)
        self.assertEqual(out["tools"][0]["function"]["name"], "noop")

    def test_keep_existing_tools(self):
        cfg = {
            "enabled": True,
            "fake_conversation": [
                {"role": "assistant", "content": "a", "reasoning_content": "think"},
            ],
        }
        existing = [
            {"type": "function", "function": {"name": "real", "parameters": {"type": "object", "properties": {}}}}
        ]
        req = {"messages": [], "tools": existing}
        out = ensure_reasoning_noop_tool(req, cfg)
        self.assertEqual(out["tools"], existing)

    def test_no_reasoning_no_inject(self):
        cfg = {
            "enabled": True,
            "fake_conversation": [{"role": "assistant", "content": "a"}],
        }
        req = {"messages": []}
        out = ensure_reasoning_noop_tool(req, cfg)
        self.assertNotIn("tools", out)

    def test_empty_tools_list_gets_injected(self):
        cfg = {
            "enabled": True,
            "fake_conversation": [
                {"role": "assistant", "content": "a", "reasoning_content": "think"},
            ],
        }
        req = {"messages": [], "tools": []}
        out = ensure_reasoning_noop_tool(req, cfg)
        self.assertEqual(len(out["tools"]), 1)

    def test_no_fake_conversation_no_inject(self):
        cfg = {"enabled": True}
        req = {"messages": []}
        out = ensure_reasoning_noop_tool(req, cfg)
        self.assertNotIn("tools", out)


if __name__ == "__main__":
    unittest.main()
