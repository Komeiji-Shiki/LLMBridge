"""Observe the selected endpoint without influencing authentication or routing."""
import copy
from functools import wraps
import inspect
from core.request_context import current_request, credential_identity


def observe_endpoint(handler):
    signature = inspect.signature(handler)
    @wraps(handler)
    async def observed(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        arguments = bound.arguments
        context = current_request.get()
        config = arguments.get('endpoint_config')
        if context and isinstance(config, dict):
            context.endpoint = copy.deepcopy(config)
            arguments['endpoint_config'] = context.endpoint
            context.model = arguments.get('model_name') or arguments.get('model') or context.model
            body = arguments.get('openai_req') or arguments.get('responses_request') or arguments.get('anthropic_req')
            if isinstance(body, dict) and not context.request_body:
                context.request_body = copy.deepcopy(body)
        return await handler(*bound.args, **bound.kwargs)
    return observed


def observe_credential(selector):
    @wraps(selector)
    async def observed(*args, **kwargs):
        key = await selector(*args, **kwargs)
        context = current_request.get()
        if context:
            context.credential_fingerprint = credential_identity(key)
        return key
    return observed
