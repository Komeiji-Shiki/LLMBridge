// JSONC token offsets also allow editing values without discarding comments.
'use strict';

function jsoncTokens(text) {
    const tokens = [];
    const pattern = /"(?:\\[\s\S]|[^"\\])*"|\/\/[^\r\n]*|\/\*[\s\S]*?\*\/|\s+|[^\s"{}\[\],:/]+|[^\s]/g;
    for (const match of text.matchAll(pattern)) {
        const value = match[0];
        if (/^\s|^\/\/|^\/\*/.test(value)) continue;
        tokens.push({ value, start: match.index, end: match.index + value.length });
    }
    return tokens;
}

function parseJsonc(content) {
    const tokens = jsoncTokens(content);
    return JSON.parse(tokens.map((token, index) => {
        if (token.value === ',' && ['}', ']'].includes(tokens[index + 1]?.value)) return '';
        return token.value;
    }).join(' '));
}

function updateJsoncValues(text, values) {
    const current = parseJsonc(text);
    if (!current || Array.isArray(current) || typeof current !== 'object') {
        throw new Error('配置根节点必须是 JSON 对象');
    }
    const tokens = jsoncTokens(text);
    const edits = [];
    const missing = { ...values };
    let depth = 0;
    for (let i = 0; i < tokens.length; i++) {
        const token = tokens[i];
        if (depth === 1 && token.value.startsWith('"') && tokens[i + 1]?.value === ':') {
            const key = JSON.parse(token.value);
            const start = tokens[i + 2].start;
            let j = i + 2, nested = 0;
            for (; j < tokens.length; j++) {
                const value = tokens[j].value;
                if (nested === 0 && [',', '}'].includes(value)) break;
                if (['{', '['].includes(value)) nested++;
                if (['}', ']'].includes(value)) nested--;
            }
            if (Object.hasOwn(values, key) && JSON.stringify(current[key]) !== JSON.stringify(values[key])) {
                edits.push({ start, end: tokens[j - 1].end, text: JSON.stringify(values[key], null, 4) });
            }
            delete missing[key];
            i = j - 1;
            continue;
        }
        if (['{', '['].includes(token.value)) depth++;
        if (['}', ']'].includes(token.value)) depth--;
    }
    if (Object.keys(missing).length) {
        const last = tokens.at(-1);
        const separator = ['{', ','].includes(tokens.at(-2)?.value) ? '' : ',';
        edits.push({ start: last.start, end: last.start, text: separator + '\n' +
            Object.entries(missing).map(([key, value]) => `    ${JSON.stringify(key)}: ${JSON.stringify(value, null, 4)}`).join(',\n') + '\n' });
    }
    for (const edit of edits.sort((a, b) => b.start - a.start)) {
        text = text.slice(0, edit.start) + edit.text + text.slice(edit.end);
    }
    parseJsonc(text);
    return text;
}
