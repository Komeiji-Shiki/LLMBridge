"""Record identity only after the existing validator succeeds.

This decorator does not authorize requests, suppress exceptions, or skip RPM.
"""
from functools import wraps
from core.request_context import current_request


def observe_authenticated_request(validator):
    @wraps(validator)
    def observed(request, model_name, *args, **kwargs):
        result = validator(request, model_name, *args, **kwargs)
        context = current_request.get()
        if context is None:
            return result
        from routes.api_routes import _extract_client_api_key, CONFIG, api_key_manager
        provided = _extract_client_api_key(request, allow_gemini_style=kwargs.get('allow_gemini_style', args[0] if args else False))
        # Validation above has already established the exact accepted credential.
        if CONFIG.get('api_key') and provided == CONFIG.get('api_key'):
            context.owner_id, context.owner_name, context.is_admin = 'admin', '管理员', True
        elif api_key_manager.has_keys():
            with api_key_manager._lock:
                key_id = api_key_manager._secret_index.get(provided)
                info = api_key_manager._keys.get(key_id, {})
                context.owner_id, context.owner_name = key_id or 'unattributed', info.get('name', '访客')
        else:
            context.owner_id, context.owner_name = 'local', '本机'
        context.authenticated = True
        return result
    return observed
