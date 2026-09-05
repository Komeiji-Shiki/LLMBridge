"""Recover native Responses output at exact visible Chat history boundaries."""
import asyncio
import copy
import hashlib
import json
import logging

from core.request_context import endpoint_identity
from core.conversation_store import conversation_store

logger = logging.getLogger(__name__)


def visible_message(message):
    """Exclude extensions mobile clients discard; keep text, media and tool IDs."""
    if not isinstance(message, dict):
        return message
    value = {key: copy.deepcopy(message[key]) for key in
             ('role', 'name', 'content', 'tool_calls', 'tool_call_id', 'function_call') if key in message}
    content = value.get('content')
    if content is None:
        value['content'] = ''
    elif isinstance(content, list) and all(isinstance(p, dict) and p.get('type') in ('text', 'input_text', 'output_text') for p in content):
        value['content'] = ''.join(str(p.get('text', '')) for p in content)
    if not value.get('tool_calls'):
        value.pop('tool_calls', None)
    return value


def history_boundaries(messages):
    digest = hashlib.sha256(b'responses-history-v1')
    for index, message in enumerate(messages):
        encoded = json.dumps(visible_message(message), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
        if isinstance(message, dict) and message.get('role') == 'assistant':
            yield index, digest.hexdigest()


class ResponsesHistory:
    def __init__(self, context, chat_request, model, endpoint_config, upstream_request):
        self.context = context
        self.chat_request = copy.deepcopy(chat_request)
        self.model = model
        self.endpoint_config = copy.deepcopy(endpoint_config)
        # Bind also to the effective instructions/tools, including custom params.
        from services.provider_capabilities import apply_native_tool_defaults
        effective = apply_native_tool_defaults(upstream_request, endpoint_config)
        self.template = {k: effective.get(k) for k in ('model', 'instructions', 'tools')}

    def scope(self):
        context = self.context
        value = [context.model, endpoint_identity(context.endpoint), context.credential_fingerprint,
                 context.session_id if context.explicit_session else None, self.template]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def restore_input(self):
        from converters.responses_bridge import convert_chat_request_to_responses
        # Always start from the client's input. A retry on another key must not
        # carry an automatically restored item from the previous attempt.
        request = copy.deepcopy(self.chat_request)
        messages = request.get('messages', [])
        boundaries = dict(history_boundaries(messages))
        try:
            records = await asyncio.to_thread(conversation_store.response_prefixes,
                self.context.owner_id, self.scope(), list(boundaries.values())) if boundaries else {}
        except Exception:
            logger.warning('Responses history lookup failed; using client history without automatic restoration')
            records = {}
        for index, prefix in boundaries.items():
            message = messages[index]
            metadata = message.get('provider_metadata')
            if isinstance(metadata, dict) and isinstance(metadata.get('responses_output'), list):
                continue
            if prefix in records:
                message['provider_metadata'] = {**(metadata if isinstance(metadata, dict) else {}),
                                                'responses_output': records[prefix]}
        return convert_chat_request_to_responses(request, self.model, self.endpoint_config)['input']

    async def remember(self, response):
        from converters.responses_bridge import convert_responses_response_to_chat
        if response.get('status') != 'completed' or response.get('error') or not response.get('output'):
            return
        chat = convert_responses_response_to_chat(response, self.model)
        message = chat['choices'][0]['message']
        history = [*self.chat_request.get('messages', []), message]
        _, prefix = list(history_boundaries(history))[-1]
        try:
            await asyncio.to_thread(conversation_store.remember_response_prefix,
                self.context.owner_id, self.scope(), prefix, response['output'])
        except Exception:
            # Never include ciphertext, message content or database payloads in logs.
            logger.warning('Responses history save failed; automatic restoration unavailable for this turn')
