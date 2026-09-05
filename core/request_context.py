"""Request-local identity, endpoint binding and phase measurements."""
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import time
import uuid


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
    upstream_request: dict = field(default_factory=dict)
    outcome: dict = field(default_factory=dict)
    responses_history: object | None = None

    def cache_session(self):
        # Never use an artifact to choose a route. Each selected upstream has
        # its own cache namespace even when the external conversation ID matches.
        value = [self.session_id, self.model, endpoint_identity(self.endpoint), self.credential_fingerprint]
        return hashlib.sha256(json.dumps(value).encode()).hexdigest()

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
        return {'request_id': self.request_id, 'prepare_ms': ms(self.started, send),
                'upstream_wait_ms': ms(send, byte), 'first_business_ms': ms(self.started, business),
                'output_ms': ms(business, end), 'total_ms': ms(self.started, end),
                'attempts': [dict(attempt) for attempt in self.attempts]}


current_request: ContextVar[RequestContext | None] = ContextVar('bridge_request', default=None)


def endpoint_identity(config: dict) -> str:
    fields = {key: config.get(key) for key in ('api_type', 'api_base_url', 'endpoint_path', 'model_id', 'provider', 'upstream_protocol')}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def credential_identity(key: str) -> str:
    return hashlib.sha256((key or '').encode()).hexdigest()
