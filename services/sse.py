"""Incremental SSE JSON reader shared by native upstream requests."""
import codecs
import json
import logging

logger = logging.getLogger(__name__)


async def iter_sse_json_events(response, tag, *, parse_bare_json=False):
    """Join data lines at event boundaries, including UTF-8/network splits.

    Partial lines are accumulated in fragments so large base64 events do not
    repeatedly copy the entire unfinished line on every network read.
    """
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    fragments = []
    data_lines = []

    def parse(payload):
        if payload.strip() == '[DONE]':
            return {'done': True}
        try:
            value = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning('[%s] Ignoring malformed SSE JSON event', tag)
            return None
        return value if isinstance(value, dict) else None

    def consume(line):
        if not line:
            if not data_lines:
                return None
            payload = '\n'.join(data_lines)
            data_lines.clear()
            return parse(payload)
        if line.startswith('data:'):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(' ') else value)
        elif line == 'data':
            data_lines.append('')
        elif parse_bare_json and line.lstrip().startswith(('{', '[')):
            return parse(line)
        return None

    async for chunk in response.content.iter_any():
        pieces = decoder.decode(chunk).split('\n')
        fragments.append(pieces[0])
        for piece in pieces[1:]:
            line = ''.join(fragments).removesuffix('\r')
            fragments.clear()
            event = consume(line)
            if event is not None:
                yield event
                if event.get('done'):
                    return
            fragments.append(piece)

    fragments.append(decoder.decode(b'', final=True))
    tail = ''.join(fragments).removesuffix('\r')
    if tail:
        event = consume(tail)
        if event is not None:
            yield event
            if event.get('done'):
                return
    event = consume('')
    if event is not None:
        yield event
