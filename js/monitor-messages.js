// 监控消息渲染：供请求详情共用，页面初始化与请求加载保留在 monitor.js。

// Anthropic 的摘要、签名与工具属于独立内容块，显示时保留块的原始顺序。
function renderAnthropicContentBlocks(blocks) {
    function disclosure(title, value, maxLen = 2000, className = '') {
        const text = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
        return `<details class="data-disclosure ${className}"><summary>${escapeHtml(title)} <span>${text.length} 字符</span></summary><pre>${renderTruncatable(text, maxLen)}</pre></details>`;
    }

    return blocks.map(block => {
        if (typeof block === 'string') {
            return `<div class="message-content native-content-block" data-content-type="text">${renderTruncatable(block)}</div>`;
        }
        if (!block || typeof block !== 'object') {
            return `<div class="message-content">${escapeHtml(String(block))}</div>`;
        }
        if (block.type === 'text') {
            return `<div class="message-content native-content-block" data-content-type="text">${renderTruncatable(block.text || '')}</div>`;
        }
        if (block.type === 'thinking') {
            const summary = block.thinking
                ? renderTruncatable(block.thinking, 2000)
                : '<span class="native-block-meta">未返回思考摘要</span>';
            const signature = block.signature
                ? disclosure('思考签名', block.signature, 500, 'native-signature')
                : '';
            return `<section class="native-content-block" data-content-type="thinking"><div class="native-block-title">思考摘要</div><div class="message-content">${summary}</div>${signature}</section>`;
        }
        if (block.type === 'redacted_thinking') {
            return `<section class="native-content-block" data-content-type="redacted_thinking"><div class="native-block-title">加密思考</div>${disclosure('加密思考数据', block.data || '', 500, 'native-signature')}</section>`;
        }
        if (block.type === 'tool_use' || block.type === 'server_tool_use') {
            return `<section class="native-content-block" data-content-type="tool_use"><div class="native-block-title">工具调用：${escapeHtml(block.name || '')}</div><div class="native-block-meta">调用 ID：${escapeHtml(block.id || '')}</div><pre class="native-tool-input">${renderTruncatable(JSON.stringify(block.input || {}, null, 2), 2000)}</pre></section>`;
        }
        if (block.type === 'tool_result') {
            const result = Array.isArray(block.content)
                ? renderAnthropicContentBlocks(block.content)
                : `<div class="message-content">${renderTruncatable(typeof block.content === 'string' ? block.content : JSON.stringify(block.content ?? '', null, 2))}</div>`;
            return `<section class="native-content-block" data-content-type="tool_result"><div class="native-block-title">工具结果${block.is_error ? '（错误）' : ''}</div><div class="native-block-meta">调用 ID：${escapeHtml(block.tool_use_id || '')}</div>${result}</section>`;
        }
        if (block.type === 'image') {
            const mediaType = block.source && block.source.media_type;
            return `<div class="message-content native-content-block" data-content-type="image">[图片内容${mediaType ? `：${escapeHtml(mediaType)}` : ''}]</div>`;
        }
        if (block.type === 'document') {
            return `<div class="message-content native-content-block" data-content-type="document">[文档内容${block.title ? `：${escapeHtml(block.title)}` : ''}]</div>`;
        }
        return disclosure(`内容块：${block.type || '未知类型'}`, block);
    }).join('');
}

function messageContentToText(msg) {
    if (!msg || !msg.content) {
        // 空字符串/ null / undefined 统一显示占位，避免消息框整块空白
        return '(空消息)';
    }

    if (typeof msg.content === 'string') {
        return msg.content;
    }

    if (Array.isArray(msg.content)) {
        return msg.content.map(part => {
            if (typeof part === 'string') {
                return part;
            }
            if (!part || typeof part !== 'object') {
                return String(part);
            }
            if (part.type === 'text') {
                return part.text || '';
            }
            if (part.type === 'image_url') {
                return '[图片内容]';
            }
            return JSON.stringify(part, null, 2);
        }).join('\n');
    }

    if (typeof msg.content === 'object') {
        return JSON.stringify(msg.content, null, 2);
    }

    return String(msg.content);
}

function coalesceResponsesRequestMessages(messages) {
    if (!Array.isArray(messages)) return [];

    const merged = [];
    let pendingAssistant = null;
    const mergedFields = new Set([
        'role', 'content', 'reasoning_content', 'reasoning_signature', 'tool_calls'
    ]);

    function flushAssistant() {
        if (pendingAssistant) {
            merged.push(pendingAssistant);
            pendingAssistant = null;
        }
    }

    function cloneAssistant(message) {
        const cloned = { ...message };
        if (Array.isArray(message.tool_calls)) cloned.tool_calls = [...message.tool_calls];
        if (Array.isArray(message.reasoning_signature)) {
            cloned.reasoning_signature = [...message.reasoning_signature];
        }
        return cloned;
    }

    function mergeTextLikeField(target, source, field, separator) {
        const incoming = source[field];
        if (incoming === undefined || incoming === null || incoming === '') return;
        const existing = target[field];
        if (existing === undefined || existing === null || existing === '') {
            target[field] = Array.isArray(incoming) ? [...incoming] : incoming;
        } else if (typeof existing === 'string' && typeof incoming === 'string') {
            target[field] = `${existing}${separator}${incoming}`;
        } else {
            const existingParts = Array.isArray(existing) ? existing : [existing];
            const incomingParts = Array.isArray(incoming) ? incoming : [incoming];
            target[field] = [...existingParts, ...incomingParts];
        }
    }

    function mergeListField(target, source, field) {
        const incoming = source[field];
        if (incoming === undefined || incoming === null) return;
        const incomingItems = Array.isArray(incoming) ? incoming : [incoming];
        if (incomingItems.length === 0) return;
        const existing = target[field];
        if (existing === undefined || existing === null) {
            target[field] = Array.isArray(incoming) ? [...incoming] : incoming;
            return;
        }
        const existingItems = Array.isArray(existing) ? existing : [existing];
        target[field] = [...existingItems, ...incomingItems];
    }

    for (const message of messages) {
        if (!message || typeof message !== 'object' || Array.isArray(message) || message.role !== 'assistant') {
            flushAssistant();
            merged.push(message);
            continue;
        }

        if (!pendingAssistant) {
            pendingAssistant = cloneAssistant(message);
            continue;
        }

        mergeTextLikeField(pendingAssistant, message, 'content', '');
        mergeTextLikeField(pendingAssistant, message, 'reasoning_content', '\n');
        mergeListField(pendingAssistant, message, 'reasoning_signature');
        mergeListField(pendingAssistant, message, 'tool_calls');

        for (const [key, value] of Object.entries(message)) {
            if (!mergedFields.has(key) && pendingAssistant[key] === undefined) {
                pendingAssistant[key] = value;
            }
        }
    }

    flushAssistant();
    return merged;
}


function renderMessageBox(msg) {
    const nativeTypes = ['thinking', 'redacted_thinking', 'tool_use', 'server_tool_use', 'tool_result', 'image', 'document'];
    const nativeContent = msg && Array.isArray(msg.content) && msg.content.some(block => block && nativeTypes.includes(block.type));
    const content = nativeContent ? '' : messageContentToText(msg);
    const toolCalls = msg && msg.tool_calls;
    const reasoning = msg && msg.reasoning_content;
    const reasoningSignature = msg && msg.reasoning_signature;
    let extraBadges = '';
    if (nativeContent) {
        const blocks = msg.content;
        if (blocks.some(block => block && (block.type === 'thinking' || block.type === 'redacted_thinking'))) {
            extraBadges += '<span class="status-badge native-message-badge">含思维链</span>';
        }
        if (blocks.some(block => block && (block.signature || block.type === 'redacted_thinking' && block.data))) {
            extraBadges += '<span class="status-badge native-message-badge">含思维链签名</span>';
        }
        if (blocks.some(block => block && (block.type === 'tool_use' || block.type === 'server_tool_use'))) {
            extraBadges += '<span class="status-badge native-message-badge">含工具调用</span>';
        }
        if (blocks.some(block => block && block.type === 'tool_result')) {
            extraBadges += '<span class="status-badge native-message-badge">含工具结果</span>';
        }
    }

    if (reasoning) {
        extraBadges += '<span class="status-badge" style="background:rgba(42,168,255,0.15);color:#7dd3fc;font-size:10px;margin-left:4px;">含思维链</span>';
    }
    if (reasoningSignature) {
        extraBadges += '<span class="status-badge" style="background:rgba(139,92,246,0.15);color:#c4b5fd;font-size:10px;margin-left:4px;">含思维链签名</span>';
    }
    if (toolCalls) {
        extraBadges += '<span class="status-badge" style="background:rgba(245,158,11,0.15);color:#fcd34d;font-size:10px;margin-left:4px;">含工具调用</span>';
    }

    let reasoningHtml = '';
    if (reasoning) {
        const rc = typeof reasoning === 'string' ? reasoning : JSON.stringify(reasoning, null, 2);
        reasoningHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(42,168,255,0.06);border:1px solid rgba(42,168,255,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#7dd3fc;font-weight:600;margin-bottom:4px;">💭 思维链 (Reasoning):</div>
                <div style="white-space:pre-wrap;word-break:break-word;color:#bae6fd;">${escapeHtml(rc.substring(0, 2000))}${rc.length > 2000 ? '... (截断)' : ''}</div>
            </div>`;
    }

    let signatureHtml = '';
    if (reasoningSignature) {
        const sig = typeof reasoningSignature === 'string' ? reasoningSignature : JSON.stringify(reasoningSignature, null, 2);
        signatureHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(139,92,246,0.06);border:1px solid rgba(139,92,246,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#c4b5fd;font-weight:600;margin-bottom:4px;">🔏 思维链签名 (Signature):</div>
                <div style="white-space:pre-wrap;word-break:break-word;color:#ddd6fe;">${escapeHtml(sig.substring(0, 500))}${sig.length > 500 ? '... (截断)' : ''}</div>
            </div>`;
    }

    let toolCallsHtml = '';
    if (toolCalls) {
        toolCallsHtml = `
            <div style="margin-top:6px;padding:8px 10px;background:rgba(245,158,11,0.06);border:1px solid rgba(245,158,11,0.2);border-radius:6px;font-size:12px;">
                <div style="color:#fcd34d;font-weight:600;margin-bottom:4px;">🔧 工具调用:</div>
                <pre style="white-space:pre-wrap;word-break:break-word;color:#fde68a;margin:0;font-size:11px;">${escapeHtml(JSON.stringify(toolCalls, null, 2))}</pre>
            </div>`;
    }

    const hasStructuredPayload = Boolean(
        reasoning || reasoningSignature || (Array.isArray(toolCalls) ? toolCalls.length : toolCalls)
    );
    const contentHtml = nativeContent ? renderAnthropicContentBlocks(msg.content) : content === '(空消息)' && hasStructuredPayload
        ? ''
        : `<div class="message-content">${escapeHtml(content)}</div>`;

    // tool_result 的协议角色仍是 user；纯工具消息的显示标签单独表达其用途。
    const protocolRole = (msg && msg.role) || 'assistant';
    const pureToolResult = nativeContent && protocolRole === 'user' && msg.content.every(block => block && block.type === 'tool_result');
    const displayRole = pureToolResult ? 'tool' : protocolRole;

    return `
        <div class="message-box">
            <div class="message-role"${pureToolResult ? ' title="Anthropic 协议角色：user"' : ''}>${escapeHtml(displayRole)}${extraBadges}</div>
            ${reasoningHtml}
            ${signatureHtml}
            ${contentHtml}
            ${toolCallsHtml}
        </div>`;
}
