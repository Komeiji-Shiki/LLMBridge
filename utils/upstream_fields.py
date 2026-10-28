"""按端点配置排除上游不支持的请求字段。"""

import logging

logger = logging.getLogger(__name__)


def configured_omissions(config: dict) -> list:
    fields = config.get("omit_request_fields", [])
    if not isinstance(fields, list) or not all(isinstance(field, str) and field for field in fields):
        raise ValueError("omit_request_fields 必须是非空字段名组成的数组。")
    return fields


def omit_configured_fields(body: dict, config: dict) -> dict:
    omitted = [field for field in configured_omissions(config) if field in body]
    for field in omitted:
        del body[field]
    if omitted:
        logger.info("[UPSTREAM_FIELDS] 端点配置不向上游发送字段: %s", ", ".join(omitted))
    return body


def omission_warnings(request: dict, config: dict) -> list:
    return [{"field": field, "applied": False,
             "message": "模型端点配置指定此字段不发送到上游。"}
            for field in configured_omissions(config) if field in request]
