"""Observe the selected endpoint and enforce model/channel quotas before any upstream call."""
import copy
from functools import wraps
import inspect
from core.request_context import current_request, credential_identity


def bind_endpoint(context, model, config):
    """把选定的模型和端点写进请求上下文，再检查模型和渠道额度。

    所有生成请求选定端点后都经过这里；额度用完时抛 402，请求不会发往上游。
    """
    from core.model_quota import model_quota
    context.model, context.endpoint = model, copy.deepcopy(config)
    model_quota.enforce(model, context.endpoint, context)


def observe_endpoint(handler):
    signature = inspect.signature(handler)
    @wraps(handler)
    async def observed(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        arguments = bound.arguments
        context = current_request.get()
        config = arguments.get('endpoint_config')
        if context and isinstance(config, dict):
            bind_endpoint(context, arguments.get('model_name') or arguments.get('model') or context.model, config)
            arguments['endpoint_config'] = context.endpoint
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
