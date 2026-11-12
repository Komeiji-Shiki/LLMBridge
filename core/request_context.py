"""Request-local identity, endpoint binding and phase measurements."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid


UPSTREAM_HEADER_EXCLUSIONS = frozenset({
    # 连接级/代理级头不能跨越到上游连接
    'connection', 'content-encoding', 'content-length', 'content-md5',
    'digest', 'expect', 'host', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade',
    # 上游响应由网关自己的 HTTP 客户端解压，压缩方式只能由它协商：转发
    # 浏览器的 zstd 等声明会让上游返回网关解不开的编码
    'accept-encoding',
    # 桥接层本地凭据和控制信息
    'authorization', 'cookie', 'api-key', 'x-api-key', 'x-goog-api-key',
    'x-web-access-key', 'x-bridge-request-id', 'x-bridge-session-id',
})

# Claude Code 随请求带的归属信息，只进监控，不改变路由。
CLIENT_TRACE_HEADERS = {
    'x-claude-code-agent-id': 'agent_id',
    'x-claude-code-parent-agent-id': 'parent_agent_id',
    'x-claude-code-agent-type': 'agent_type',
    'x-claude-code-request-class': 'request_class',
    'x-claude-code-prompt-id': 'prompt_id',
    'x-claude-code-compaction': 'compaction',
    'x-claude-code-context-compacted': 'context_compacted',
}


# 准备阶段（进入网关到开始调用上游）的分段点，按发生顺序；某条链路没打的点并进下一段。
# receive 收完请求体，parse 解析出 JSON，route 鉴权、选线路和入口协议转换，convert 之后到调用上游。
PREPARE_STAGES = (('receive_ms', 'body_received'), ('parse_ms', 'parsed'),
                  ('route_ms', 'recorded'), ('convert_ms', 'upstream_start'))


@dataclass
class RequestContext:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    owner_id: str = 'unattributed'
    owner_name: str = '未归属'
    is_admin: bool = False
    authenticated: bool = False
    explicit_session: bool = False
    model: str = ''
    endpoint: dict = field(default_factory=dict)
    request_body: dict = field(default_factory=dict)
    started: float = field(default_factory=time.perf_counter)
    marks: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)
    transport_depth: int = 0
    credential_fingerprint: str = ''
    artifacts: dict = field(default_factory=dict)
    upstream_headers: dict = field(default_factory=dict)
    upstream_response_headers: dict = field(default_factory=dict)
    upstream_request: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    responses_history: object | None = None
    gemini_signature_recovery: object | None = None

    def cache_session(self):
        # Never use an artifact to choose a route. Each selected upstream has
        # its own cache namespace even when the external conversation ID matches.
        # 只有 X-Bridge-Session-ID 进一步隔离；其余请求共用“调用方 + 线路”这一级，
        # 签名按原文或调用 ID 精确匹配，共用命名空间不会串到别的对话。
        session = self.session_id if self.explicit_session else None
        return cache_scope(session, self.model, endpoint_identity(self.endpoint), self.credential_fingerprint)

    def mark(self, name):
        self.marks.setdefault(name, time.perf_counter())

    def snapshot(self):
        now = time.perf_counter()
        send = self.marks.get('upstream_start')
        byte = self.marks.get('first_byte')
        business = self.marks.get('first_business')
        end = self.marks.get('finished', now)
        def ms(start, stop):
            return round(max(0, stop - start) * 1000, 2) if start is not None and stop is not None else None
        stages, previous = {}, self.started
        for name, mark in PREPARE_STAGES:
            reached = self.marks.get(mark)
            if reached is not None:
                stages[name] = ms(previous, reached)
                previous = reached
        return {'request_id': self.request_id, 'prepare_ms': ms(self.started, send),
                'prepare_stages': stages,
                'upstream_wait_ms': ms(send, byte), 'upstream_build_ms': ms(send, self.marks.get('upstream_post')),
                'first_business_ms': ms(self.started, business),
                'output_ms': ms(business, end), 'total_ms': ms(self.started, end),
                'attempts': [dict(attempt) for attempt in self.attempts]}


current_request: ContextVar[RequestContext | None] = ContextVar('bridge_request', default=None)


def endpoint_identity(config: dict) -> str:
    fields = {key: config.get(key) for key in ('api_type', 'api_base_url', 'endpoint_path', 'model_id', 'provider', 'upstream_protocol')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def cache_scope(session, model, endpoint, credential) -> str:
    """缓存命名空间；endpoint 和 credential 是 endpoint_identity / credential_identity 的结果。"""
    return hashlib.sha256(json.dumps([session, model, endpoint, credential]).encode()).hexdigest()


def credential_identity(key: str) -> str:
    return hashlib.sha256((key or '').encode()).hexdigest()
