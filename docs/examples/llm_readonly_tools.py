"""Opt-in tool prototype. Not registered with the running gateway.

Adapters supply a model snapshot, an authenticated principal and a statistics
callback. No shell, network fetching, configuration writes or raw log access.
"""
from dataclasses import dataclass
from datetime import date
from typing import Awaitable, Callable
import asyncio


@dataclass
class ToolContext:
    models: dict
    allowed_models: set[str] | None
    is_admin: bool
    usage_reader: Callable[[str, str], Awaitable[dict]]


TOOLS = [
    {"type": "function", "function": {
        "name": "bridge_list_models", "description": "List configured model aliases visible to the caller.",
        "strict": True, "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "bridge_describe_model", "description": "Inspect a visible model's protocol and routing count without credentials.",
        "strict": True, "parameters": {"type": "object", "properties": {"model": {"type": "string"}},
                                        "required": ["model"], "additionalProperties": False}}},
    {"type": "function", "function": {
        "name": "bridge_usage_summary", "description": "Admin-only aggregate usage over an inclusive date range.",
        "strict": True, "parameters": {"type": "object", "properties": {
            "start_date": {"type": "string", "format": "date"}, "end_date": {"type": "string", "format": "date"}},
            "required": ["start_date", "end_date"], "additionalProperties": False}}},
]


def visible_models(context: ToolContext) -> dict:
    visible = {}
    for name, raw in context.models.items():
        if context.allowed_models is not None and name not in context.allowed_models:
            continue
        primary = raw[0] if isinstance(raw, list) and raw else raw
        if isinstance(primary, dict) and not primary.get('archived'):
            visible[name] = raw
    return visible


async def execute_readonly_tool(name: str, arguments: dict, context: ToolContext) -> dict:
    definitions = {item['function']['name']: item['function']['parameters'] for item in TOOLS}
    schema = definitions.get(name)
    if schema is None:
        raise ValueError('Unknown tool')
    if not isinstance(arguments, dict) or set(arguments) != set(schema['properties']):
        raise ValueError('Unexpected or missing arguments')
    if name == 'bridge_list_models':
        return {'models': list(visible_models(context))}
    if name == 'bridge_describe_model':
        model = arguments['model']
        if not isinstance(model, str):
            raise ValueError('model must be a string')
        raw = visible_models(context).get(model)
        if raw is None:
            raise PermissionError('Model unavailable')
        endpoints = raw if isinstance(raw, list) else [raw]
        return {'model': model, 'endpoints': len(endpoints),
                'protocols': sorted({endpoint.get('api_type', 'legacy') for endpoint in endpoints}),
                'note': 'Configured protocols; model capabilities have not been probed.'}
    # Existing statistics aggregate all callers; never expose them to a guest.
    if not context.is_admin:
        raise PermissionError('Aggregate statistics require administrator access')
    start = date.fromisoformat(arguments['start_date'])
    end = date.fromisoformat(arguments['end_date'])
    if end < start:
        raise ValueError('end_date must be on or after start_date')
    return await asyncio.wait_for(context.usage_reader(start.isoformat(), end.isoformat()), timeout=10)
