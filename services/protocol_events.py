"""Classify protocol events without changing their wire representation."""
import json


def payloads(chunk):
    if isinstance(chunk, dict):
        return [chunk]
    text = chunk.decode('utf-8', errors='replace') if isinstance(chunk, bytes) else str(chunk)
    values = []
    if text.lstrip().startswith(('{', '[')):
        try:
            return [json.loads(text)]
        except ValueError:
            return []
    for block in text.replace('\r\n', '\n').split('\n\n'):
        data = '\n'.join(line[5:].lstrip(' ') for line in block.splitlines() if line.startswith('data:'))
        if data.strip() == '[DONE]':
            values.append({'done': True})
        elif data:
            try:
                values.append(json.loads(data))
            except ValueError:
                pass
    return values


def error_status(value):
    if not isinstance(value, dict):
        return None
    error = value.get('error')
    kind = value.get('type') or value.get('event_type') or ''
    if kind == 'response.failed':
        error = (value.get('response') or {}).get('error') or {'code': 502}
    if error is None and kind != 'error':
        return None
    error = error if isinstance(error, dict) else {}
    code = value.get('_http_status') or error.get('code') or error.get('status')
    try:
        return int(code)
    except (TypeError, ValueError):
        kind = str(error.get('type') or value.get('code') or '')
        return 429 if 'rate_limit' in kind else 504 if 'timeout' in kind else 502


def is_terminal(value):
    if not isinstance(value, dict):
        return False
    if (value.get('promptFeedback') or {}).get('blockReason'):
        return True
    if value.get('done') or (value.get('type') or value.get('event_type')) in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.complete', 'interaction.completed'):
        return True
    if any(candidate.get('finishReason') for candidate in value.get('candidates', []) if isinstance(candidate, dict)):
        return True
    return any(choice.get('finish_reason') for choice in value.get('choices', []) if isinstance(choice, dict))


def is_business_event(value):
    if not isinstance(value, dict) or error_status(value) is not None or value.get('done'):
        return False
    kind = value.get('type') or value.get('event_type') or ''
    if kind in ('ping', 'response.created', 'response.in_progress', 'message_start', 'interaction.start', 'interaction.created', 'interaction.status_update'):
        return False
    if kind in ('response.completed', 'response.incomplete', 'message_stop', 'interaction.complete'):
        return True
    if kind == 'response.output_item.added':
        item = value.get('item') or {}
        return item.get('type') not in ('message', 'reasoning') or bool(item.get('encrypted_content') or item.get('content') or item.get('summary'))
    if kind == 'content_block_start':
        block = value.get('content_block') or {}
        return block.get('type') not in ('text', 'thinking') or bool(block.get('text') or block.get('thinking'))
    if kind in ('response.content_part.added', 'response.reasoning_summary_part.added', 'response.reasoning_part.added'):
        part = value.get('part') or {}
        return bool(part.get('text') or part.get('refusal'))
    if kind == 'content_block_stop':
        return False
    if kind == 'content_block_delta':
        delta = value.get('delta') or {}
        return any(delta.get(key) for key in ('text', 'thinking', 'partial_json', 'signature'))
    if 'choices' in value:
        for choice in value.get('choices') or []:
            delta = choice.get('delta') or choice.get('message') or {}
            if any(delta.get(key) for key in ('content', 'reasoning_content', 'reasoning', 'reasoning_details', 'tool_calls', 'function_call', 'refusal', 'audio')):
                return True
        return False
    if 'candidates' in value:
        return any(any(part.get(key) for key in ('text', 'thoughtSignature', 'functionCall', 'inlineData', 'fileData', 'executableCode', 'codeExecutionResult'))
                   for candidate in value.get('candidates') or [] for part in (candidate.get('content') or {}).get('parts') or [] if isinstance(part, dict))
    if kind.endswith('.delta'):
        return bool(value.get('delta'))
    if kind:
        # Unknown typed provider/tool events commit the stream conservatively.
        return True
    return bool(value.get('content') or value.get('output') or value.get('steps') or value.get('promptFeedback'))
