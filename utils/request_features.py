"""提取日志列表需要的布尔标记，避免列表传输完整上下文。"""


def request_features(record):
    messages = list(record.get('request_messages') or [])
    if isinstance(record.get('response_message'), dict):
        messages.append(record['response_message'])
    reasoning = bool(record.get('reasoning_content'))
    tools = bool(record.get('response_tool_calls'))
    for message in messages:
        if not isinstance(message, dict):
            continue
        reasoning |= bool(message.get('reasoning_content'))
        tools |= bool(message.get('tool_calls')) or message.get('role') == 'tool'
        content = message.get('content')
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    reasoning |= part.get('type') in ('thinking', 'redacted_thinking', 'reasoning')
                    tools |= part.get('type') in ('tool_use', 'tool_result', 'server_tool_use') or bool(part.get('functionCall') or part.get('functionResponse'))
    return {'has_reasoning': reasoning or record.get('has_reasoning', False),
            'has_tool_calls': tools or record.get('has_tool_calls', False)}
