// ==UserScript==
// @name         Discord ChannelHistory Harvester (Stealth Mode)
// @namespace    https://graywill.local/
// @version      1.1.0
// @description  寄生于已登录的Discord网页端，自动拓印真实请求头，拟人低速抓取当前频道近N天消息
// @author       Graywill
// @match        https://discord.com/*
// @run-at       document-start
// @grant        unsafeWindow
// ==/UserScript==

(function() {
    'use strict';

    const pageWin = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;

    // --- 1. 无感拓印官方前端的真实请求头 ---
    const capturedHeaders = {};
    let isHeaderReady = false;

    const origOpen = pageWin.XMLHttpRequest.prototype.open;
    const origSetHeader = pageWin.XMLHttpRequest.prototype.setRequestHeader;

    pageWin.XMLHttpRequest.prototype.open = function (method, url, ...rest) {
        this._gwUrl = typeof url === 'string' ? url : '';
        return origOpen.call(this, method, url, ...rest);
    };

    pageWin.XMLHttpRequest.prototype.setRequestHeader = function (header, value) {
        if (this._gwUrl && this._gwUrl.includes('/api/v9/')) {
            const k = header.toLowerCase();
            if ([
                'authorization',
                'x-super-properties',
                'x-discord-locale',
                'x-discord-timezone',
                'x-debug-options'
            ].includes(k)) {
                capturedHeaders[header] = value;
                if (k === 'authorization' && value && !isHeaderReady) {
                    isHeaderReady = true;
                    updateAuthBadge();
                }
            }
        }
        return origSetHeader.call(this, header, value);
    };

    // --- 2. 状态与工具函数 ---
    let isRunning = false;
    let shouldStop = false;
    let collectedMessages = [];

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const randInt = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;

    function getCurrentChannelId() {
        const m = location.pathname.match(/^\/channels\/(?:@me|\d+)\/(\d+)/);
        return m ? m[1] : null;
    }

    function snowflakeToMs(idStr) {
        return Number((BigInt(idStr) >> 22n) + 1420070400000n);
    }

    function requestPageXHR(url) {
        return new Promise((resolve, reject) => {
            const xhr = new pageWin.XMLHttpRequest();
            origOpen.call(xhr, 'GET', url, true);
            xhr.withCredentials = true;
            for (const [k, v] of Object.entries(capturedHeaders)) {
                origSetHeader.call(xhr, k, v);
            }
            xhr.onload = () => {
                let data = null;
                try {
                    data = JSON.parse(xhr.responseText);
                } catch (e) {}
                resolve({ status: xhr.status, data });
            };
            xhr.onerror = () => reject(new Error('XHR Network Error'));
            xhr.send();
        });
    }

    // --- 3. 核心拟人抓取循环 ---
    async function startHarvest() {
        if (isRunning) return;
        const channelId = getCurrentChannelId();
        if (!channelId) {
            logStatus('错误：未检测到频道ID，请先点进一个具体频道');
            return;
        }
        if (!isHeaderReady) {
            logStatus('等待捕获请求头...请随便点一下左侧频道或滚动聊天记录');
            return;
        }

        const days = parseFloat(document.getElementById('gw-days-input').value) || 7;
        const cutoffMs = Date.now() - days * 86400 * 1000;
        const maxPages = parseInt(document.getElementById('gw-maxpages-input').value, 10) || 300;

        isRunning = true;
        shouldStop = false;
        collectedMessages = [];
        const seenIds = new Set();
        updateButtons();

        let beforeId = null;
        let pageCount = 0;
        let nextBatchPause = randInt(12, 18);

        logStatus('开始抓取频道 [' + channelId + ']，目标：近 ' + days + ' 天');

        try {
            while (!shouldStop && pageCount < maxPages) {
                const query = beforeId ? ('?before=' + beforeId + '&limit=50') : '?limit=50';
                const url = 'https://discord.com/api/v9/channels/' + channelId + '/messages' + query;

                const res = await requestPageXHR(url);

                if (res.status === 429) {
                    const retrySec = (res.data && res.data.retry_after) ? res.data.retry_after : 10;
                    const waitMs = Math.ceil((retrySec + 3) * 1000);
                    logStatus('触发限流(429)，退避等待 ' + (waitMs / 1000).toFixed(1) + 's...');
                    await sleep(waitMs);
                    continue;
                }
                if (res.status !== 200 || !Array.isArray(res.data)) {
                    logStatus('异常状态码 ' + res.status + '，已安全中止');
                    break;
                }

                const batch = res.data;
                if (batch.length === 0) {
                    logStatus('已到达频道最早消息');
                    break;
                }

                pageCount++;
                let reachedCutoff = false;

                for (const msg of batch) {
                    const msgMs = snowflakeToMs(msg.id);
                    if (msgMs < cutoffMs) {
                        reachedCutoff = true;
                        break;
                    }
                    if (!seenIds.has(msg.id)) {
                        seenIds.add(msg.id);
                        collectedMessages.push(msg);
                    }
                }
                beforeId = batch[batch.length - 1].id;
                const oldestDateStr = new Date(snowflakeToMs(beforeId)).toLocaleString('zh-CN', { hour12: false });
                logStatus('第 ' + pageCount + ' 页 | 已收 ' + collectedMessages.length + ' 条 | 翻至 ' + oldestDateStr);

                if (reachedCutoff || batch.length < 50) {
                    logStatus('完成！已覆盖近 ' + days + ' 天，共 ' + collectedMessages.length + ' 条消息');
                    break;
                }

                if (pageCount >= nextBatchPause) {
                    const restMs = randInt(8000, 14000);
                    logStatus('批次小憩 ' + (restMs / 1000).toFixed(1) + 's (已抓 ' + collectedMessages.length + ' 条)...');
                    await sleep(restMs);
                    nextBatchPause = pageCount + randInt(12, 18);
                } else {
                    await sleep(randInt(1800, 4000));
                }
            }
        } catch (err) {
            logStatus('出错中止: ' + err.message);
        } finally {
            collectedMessages.sort((a, b) => (BigInt(a.id) > BigInt(b.id) ? 1 : -1));
            isRunning = false;
            updateButtons();
            if (shouldStop) {
                logStatus('已手动停止，保留了 ' + collectedMessages.length + ' 条消息');
            }
        }
    }

    // --- 4. 导出数据 ---
    function downloadBlob(content, filename, mime) {
        const blob = new Blob([content], { type: mime });
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    }

    function exportJSON() {
        if (!collectedMessages.length) return alert('还没有抓到消息呢~');
        const ch = getCurrentChannelId() || 'channel';
        const filename = 'discord_' + ch + '_' + collectedMessages.length + 'msgs.json';
        downloadBlob(JSON.stringify(collectedMessages, null, 2), filename, 'application/json;charset=utf-8');
    }

    function exportReadableText() {
        if (!collectedMessages.length) return alert('还没有抓到消息呢~');
        const ch = getCurrentChannelId() || 'channel';
        const lines = collectedMessages.map((m) => {
            const time = new Date(snowflakeToMs(m.id)).toLocaleString('zh-CN', { hour12: false });
            const author = m.author ? (m.author.global_name || m.author.username) : 'Unknown';
            const replyInfo = m.referenced_message
                ? ('[回复 @' + (m.referenced_message.author && m.referenced_message.author.username ? m.referenced_message.author.username : '?') + ']')
                : '';
            const attachments = (m.attachments && m.attachments.length)
                ? ('[附件: ' + m.attachments.map((a) => a.url).join(' , ') + ']')
                : '';
            return '[' + time + '] ' + author + replyInfo + ': ' + (m.content || '') + attachments;
        });
        const filename = 'discord_' + ch + '_' + collectedMessages.length + 'msgs.txt';
        downloadBlob(lines.join('\n'), filename, 'text/plain;charset=utf-8');
    }

    // --- 5. 纯行内样式 UI（绕过 Discord CSP style-src 拦截） ---
    function mountUI() {
        if (!document.body || document.getElementById('gw-harvester-root')) return;

        const root = document.createElement('div');
        root.id = 'gw-harvester-root';
        root.style.cssText = 'position:fixed;right:16px;bottom:16px;width:300px;background:#0d0e11;color:#e2e4e9;border:1px solid #2e3138;border-left:4px solid #d93829;font-family:Consolas,monospace;font-size:12px;z-index:2147483647;box-shadow:6px 6px 0px rgba(0,0,0,0.65);user-select:none;';

        const rowStyle = 'display:flex;justify-content:space-between;align-items:center;gap:8px;';
        const inputStyle = 'width:80px;background:#16181d;color:#ffffff;border:1px solid #3a3e47;padding:4px 6px;font-family:inherit;font-size:12px;outline:none;border-radius:0;';
        const btnStyle = 'flex:1;background:#1c1f26;color:#e2e4e9;border:1px solid #3a3e47;padding:6px 8px;font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;border-radius:0;';
        const primaryBtnStyle = 'flex:1;background:#d93829;color:#ffffff;border:1px solid #d93829;padding:6px 8px;font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;border-radius:0;';

        root.innerHTML =
            '<div id="gw-harvester-header" title="点击折叠/展开" style="display:flex;justify-content:space-between;align-items:center;padding:8px 10px;background:#16181d;border-bottom:1px solid #2e3138;cursor:pointer;font-weight:700;letter-spacing:0.5px;">' +
                '<span>DC_HARVESTER // 01</span>' +
                '<span id="gw-auth-badge" style="display:inline-block;padding:1px 5px;font-size:10px;background:#3a1d1d;color:#ff6b6b;border:1px solid #ff6b6b;">WAIT_HDR</span>' +
            '</div>' +
            '<div id="gw-harvester-body" style="padding:10px;display:flex;flex-direction:column;gap:8px;">' +
                '<div style="' + rowStyle + '">' +
                    '<span>回溯天数 (Days)</span>' +
                    '<input id="gw-days-input" type="number" value="7" min="0.5" max="365" step="1" style="' + inputStyle + '">' +
                '</div>' +
                '<div style="' + rowStyle + '">' +
                    '<span>页数上限 (×50条)</span>' +
                    '<input id="gw-maxpages-input" type="number" value="200" min="1" max="2000" style="' + inputStyle + '">' +
                '</div>' +
                '<div style="' + rowStyle + '">' +
                    '<button id="gw-start-btn" style="' + primaryBtnStyle + '">START 开始</button>' +
                    '<button id="gw-stop-btn" disabled style="' +btnStyle + 'opacity:0.4;">STOP 停止</button>' +
                '</div>' +
                '<div style="' + rowStyle + '">' +
                    '<button id="gw-export-json" style="' + btnStyle + '">导出 JSON</button>' +
                    '<button id="gw-export-txt" style="' + btnStyle + '">导出 TXT</button>' +
                '</div>' +
                '<div id="gw-status-box" style="background:#131519;border:1px solid #252830;padding:6px 8px;color:#a0a6b5;line-height:1.4;min-height:34px;word-break:break-all;">就绪。进入目标频道后点击 START。</div>' +
            '</div>';

        document.body.appendChild(root);
        const bodyEl = document.getElementById('gw-harvester-body');
        document.getElementById('gw-harvester-header').addEventListener('click', () => {
            bodyEl.style.display = bodyEl.style.display === 'none' ? 'flex' : 'none';
        });
        document.getElementById('gw-start-btn').addEventListener('click', startHarvest);
        document.getElementById('gw-stop-btn').addEventListener('click', () => {
            shouldStop = true;
            logStatus('正在停止（等待当前请求返回）...');
        });
        document.getElementById('gw-export-json').addEventListener('click', exportJSON);
        document.getElementById('gw-export-txt').addEventListener('click', exportReadableText);

        updateAuthBadge();
    }

    function updateAuthBadge() {
        const badge = document.getElementById('gw-auth-badge');
        if (!badge) return;
        if (isHeaderReady) {
            badge.textContent = 'HDR_LOCKED';
            badge.style.background = '#133321';
            badge.style.color = '#3dd68c';
            badge.style.borderColor = '#3dd68c';
        }
    }

    function updateButtons() {
        const startBtn = document.getElementById('gw-start-btn');
        const stopBtn = document.getElementById('gw-stop-btn');
        if (!startBtn || !stopBtn) return;
        startBtn.disabled = isRunning;
        startBtn.style.opacity = isRunning ? '0.4' : '1';
        stopBtn.disabled = !isRunning;
        stopBtn.style.opacity = !isRunning ? '0.4' : '1';
    }

    function logStatus(text) {
        const box = document.getElementById('gw-status-box');
        if (box) box.textContent = text;
    }

    // 定时守护挂载，防止 Discord 初始化时清空 body
    setInterval(mountUI, 1000);
})();
