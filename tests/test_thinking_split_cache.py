"""思考分隔符切分前原文缓存测试（临时 SQLite，无网络依赖）。"""
import tempfile
import unittest
from pathlib import Path

from core.conversation_store import ConversationStore
from core.request_context import RequestContext, current_request
from services import thinking_split_cache


class ThinkingSplitCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_store = thinking_split_cache.conversation_store
        thinking_split_cache.conversation_store = ConversationStore(
            path=Path(self.tmp.name) / 'conversations.db')
        context = RequestContext(owner_id='owner', authenticated=True,
                                 model='gemini-test', endpoint={'api_type': 'gemini_native'})
        self.token = current_request.set(context)

    def tearDown(self):
        current_request.reset(self.token)
        thinking_split_cache.conversation_store = self.original_store
        self.tmp.cleanup()

    def test_round_trip_ignores_surrounding_whitespace(self):
        original = '<analysis_channel>思考</analysis_channel>正文'
        thinking_split_cache.remember_split_original('正文', original)

        self.assertEqual(thinking_split_cache.lookup_split_original('正文'), original)
        self.assertEqual(thinking_split_cache.lookup_split_original('  正文\n'), original)
        self.assertIsNone(thinking_split_cache.lookup_split_original('别的正文'))

    def test_empty_or_shorter_original_is_not_stored(self):
        thinking_split_cache.remember_split_original('', 'x')
        thinking_split_cache.remember_split_original('正文', '正文')
        self.assertIsNone(thinking_split_cache.lookup_split_original('正文'))

    def test_restore_only_body_only_assistant_messages(self):
        original = '<analysis_channel>思考</analysis_channel>正文'
        thinking_split_cache.remember_split_original('正文', original)

        messages = [
            {'role': 'user', 'content': '正文'},
            {'role': 'assistant', 'content': '正文'},
            {'role': 'assistant', 'content': '正文', 'reasoning_content': '思考'},
            {'role': 'assistant', 'content': '正文',
             'provider_metadata': {'gemini_parts': [{'text': '正文'}]}},
        ]
        restored = thinking_split_cache.restore_history_messages(messages)

        self.assertEqual(restored[0]['content'], '正文')
        self.assertEqual(restored[1]['content'], original)
        self.assertEqual(restored[2]['content'], '正文')
        self.assertEqual(restored[3]['content'], '正文')
        # 原列表不被修改
        self.assertEqual(messages[1]['content'], '正文')

    def test_unauthenticated_context_does_not_cache(self):
        current_request.reset(self.token)
        self.token = current_request.set(RequestContext(owner_id='owner'))
        thinking_split_cache.remember_split_original('正文', '<analysis_channel>思考</analysis_channel>正文')
        self.assertIsNone(thinking_split_cache.lookup_split_original('正文'))


if __name__ == '__main__':
    unittest.main()
