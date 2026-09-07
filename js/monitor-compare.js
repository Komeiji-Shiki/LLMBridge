/** Cache comparison state and presentation, isolated from live monitor updates. */
const MonitorCompare = (() => {
    'use strict';
    // ================= TPS 与缓存对比 =================
    // 口径与后端 utils/prompt_cache_compare.compute_tps 一致：
    // 输出阶段平均速度 = 输出 tokens / output_ms，端到端 = 输出 tokens / duration。
    function computeTps(details) {
        const outputTokens = Number(details?.output_tokens) || 0;
        const durationRaw = Number(details?.duration);
        const duration = Number.isFinite(durationRaw) && durationRaw > 0 ? durationRaw : null;
        const timings = (details && details.timings) || {};
        const outputMsRaw = Number(timings.output_ms);
        const outputS = Number.isFinite(outputMsRaw) && outputMsRaw > 0 ? outputMsRaw / 1000 : null;
        const ttftMsRaw = Number(timings.first_business_ms);
        const ttftS = Number.isFinite(ttftMsRaw) && ttftMsRaw > 0 ? ttftMsRaw / 1000 : null;
        const decodeTps = (outputS && outputTokens) ? outputTokens / outputS : null;
        const e2eTps = (duration && outputTokens) ? outputTokens / duration : null;
        const caveat = (details.streaming ?? details.stream) === false
            ? '非流式响应无法测得解码速度；输出阶段速度仅为收尾阶段比值，请参考端到端速度。'
            : (decodeTps != null && duration && outputS && outputS < duration / 10)
                ? '输出阶段远短于总耗时，其平均速度可能偏高，请结合端到端速度判断。' : null;
        return { outputTokens, durationS: duration, outputS, ttftS, decodeTps, e2eTps, caveat };
    }

    // 详情页“生成速度”行
    function renderTpsRowHtml(tps) {
        if (!tps || !tps.outputTokens || (tps.decodeTps == null && tps.e2eTps == null)) {
            return '— <span style="font-size:12px;opacity:.7;">（无输出 token 或耗时数据）</span>';
        }
        const parts = [];
        if (tps.decodeTps != null) parts.push(`输出阶段 <strong>${escapeHtml(tps.decodeTps.toFixed(1))}</strong> tok/s`);
        if (tps.e2eTps != null) parts.push(`端到端 <strong>${escapeHtml(tps.e2eTps.toFixed(1))}</strong> tok/s`);
        const meta = [`输出 ${tps.outputTokens.toLocaleString()} tok`];
        if (tps.outputS != null) meta.push(`输出阶段 ${tps.outputS.toFixed(1)}s`);
        if (tps.durationS != null) meta.push(`总耗时 ${tps.durationS.toFixed(1)}s`);
        if (tps.ttftS != null) meta.push(`首业务事件 ${tps.ttftS.toFixed(2)}s`);
        let html = parts.join(' · ') + `<span style="font-size:12px;opacity:.7;">（${escapeHtml(meta.join(' / '))}）</span>`;
        html += '<div class="tps-note">输出阶段平均速度包含业务事件及传输耗时；首业务事件可能是推理或工具事件。</div>';
        if (tps.caveat) html += `<div style="font-size:12px;color:#fbbf24;margin-top:4px;">⚠️ ${escapeHtml(tps.caveat)}</div>`;
        return html;
    }

    // 对比选择集（最多 2 条，按勾选顺序）
    let compareSelection = [];
    let compareRequestVersion = 0;

    function updateCompareButton() {
        const btn = document.getElementById('compare-btn');
        btn.textContent = `对比选中 (${compareSelection.length}/2)`;
        btn.disabled = compareSelection.length !== 2;
        const boxes = [...document.querySelectorAll('.compare-check')];
        boxes.forEach(box => {
            box.checked = compareSelection.includes(box.dataset.requestId);
            box.closest('tr').classList.toggle('is-selected', box.checked);
        });
        const visible = boxes.filter(box => !box.disabled);
        const checked = visible.filter(box => box.checked);
        const master = document.getElementById('compare-select-all');
        master.disabled = !visible.length;
        master.checked = checked.length > 0 && checked.length === Math.min(2, visible.length);
        master.indeterminate = checked.length > 0 && !master.checked;
        const hidden = compareSelection.filter(id => !visible.some(box => box.dataset.requestId === id)).length;
        document.getElementById('compare-hint').textContent = hidden
            ? `已选 ${compareSelection.length} 条，其中 ${hidden} 条在其他页或筛选范围外`
            : compareSelection.length === 2 ? '已选两条，按请求时间先后对比' : '勾选两条请求，检查缓存命中差异';
        document.getElementById('compare-clear').disabled = !compareSelection.length;
        document.getElementById('compare-slots').innerHTML = compareSelection.map((id, index) =>
            `<span class="compare-slot" title="${escapeHtml(id)}">${index + 1} / ${escapeHtml(shortReqId(id))}` +
            `<button type="button" data-request-id="${escapeHtml(id)}" onclick="MonitorCompare.toggleCompareSelection(this.dataset.requestId, false)" aria-label="移除已选请求 ${index + 1}">×</button></span>`
        ).join('');
        const hint = document.getElementById('compare-current-hint');
        if (hint) hint.textContent = `已选 ${compareSelection.length}/2`;
    }

    function toggleCompareSelection(requestId, checked) {
        if (!requestId) return;
        compareSelection = compareSelection.filter(id => id !== requestId);
        if (checked) compareSelection = [...compareSelection, requestId].slice(-2);
        updateCompareButton();
    }

    function toggleCompareSelectAll(master) {
        const boxes = [...document.querySelectorAll('.compare-check:not([disabled])')];
        compareSelection = master.checked ? boxes.slice(0, 2).map(box => box.dataset.requestId) : [];
        updateCompareButton();
    }

    function clearCompareSelection() {
        compareSelection = [];
        updateCompareButton();
    }

    function compareWithCurrent(requestId) {
        if (!requestId) return;
        if (!compareSelection.includes(requestId)) toggleCompareSelection(requestId, true);
        if (compareSelection.length === 2) {
            closeModal();
            document.getElementById('compare-btn').focus();
            openCompareSelected();
        }
    }

    function openCompareSelected() {
        if (compareSelection.length !== 2) return;
        openCompareModal(compareSelection[0], compareSelection[1]);
    }

    async function openCompareModal(a, b) {
        const modal = document.getElementById('compareModal');
        const body = document.getElementById('compareBody');
        if (!modal || !body) return;
        const version = ++compareRequestVersion;
        if (!modal._returnFocus) modal._returnFocus = document.activeElement;
        modal.style.display = 'block';
        modal.querySelector('.close').focus();
        modal.querySelector('.modal-content').scrollTop = 0;
        document.body.classList.add('detail-open');
        body.innerHTML = '<div class="empty-state">加载中...</div>';
        try {
            const resp = await apiGet(`/api/monitor/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
            const data = await resp.json();
            if (version !== compareRequestVersion) return;
            renderCompareResult(data);
        } catch (e) {
            if (version !== compareRequestVersion) return;
            body.innerHTML = `<div class="empty-state">对比失败: ${escapeHtml(e.message)}</div>`;
        }
    }

    function closeCompareModal() {
        ++compareRequestVersion;
        const modal = document.getElementById('compareModal');
        if (modal) {
            modal.style.display = 'none';
            modal._returnFocus?.focus();
            modal._returnFocus = null;
        }
        if (document.getElementById('detailModal')?.style.display !== 'block') {
            document.body.classList.remove('detail-open');
        }
    }

    function formatCacheNum(value) {
        return value == null ? '—' : Number(value).toLocaleString();
    }

    function formatCompareTime(ts) {
        if (!ts) return '—';
        try {
            return new Date(Number(ts) * 1000).toLocaleString();
        } catch {
            return '—';
        }
    }

    function shortReqId(id) {
        if (!id) return '—';
        return String(id).length > 12 ? String(id).substring(0, 8) + '...' : String(id);
    }

    // 在差异字符位置插入标记
    function renderDiffContext(text, absOffset) {
        if (text == null) return '—';
        if (absOffset == null) return escapeHtml(text);
        // Python offsets count Unicode code points; JS slice counts UTF-16 code units.
        const chars = Array.from(text);
        const rel = Math.min(absOffset, 140);
        return escapeHtml(chars.slice(0, rel).join('')) +
            '<span class="diff-mark" title="首个差异位置">⇄</span>' +
            escapeHtml(chars.slice(rel).join(''));
    }

    function renderTpsMini(label, tps) {
        if (!tps) return '';
        return `<div class="compare-tps"><span>${escapeHtml(label)}</span>` +
            `<strong>输出阶段 ${escapeHtml(tps.decode_tps != null ? tps.decode_tps.toFixed(1) : '—')}</strong>` +
            `<strong>端到端 ${escapeHtml(tps.e2e_tps != null ? tps.e2e_tps.toFixed(1) : '—')}</strong>` +
            `<span>tok/s${tps.ttft_s != null ? ` · 首业务事件 ${escapeHtml(String(tps.ttft_s))}s` : ''}</span>${tps.caveat ? `<p class="tps-note">${escapeHtml(tps.caveat)}</p>` : ''}</div>`;
    }

    function renderCompareResult(data) {
        const body = document.getElementById('compareBody');
        if (!body) return;
        const summary = data.summary || {};
        const oldSum = summary.old || {};
        const newSum = summary.new || {};
        const messages = data.messages || {};
        const tools = data.tools || {};
        const params = data.params || {};
        const identity = data.identity || {};
        const tps = data.tps || {};
        const oldRef = data.old_ref || {};
        const newRef = data.new_ref || {};
        const delta = summary.cached_delta || 0;

        const modelWarn = data.models_match ? '' :
            '<div class="compare-warn">⚠️ 两条请求的模型不一致，对比仅供参考（不同模型的缓存空间通常不共享）。</div>';

        const first = messages.first_difference;
        let messagesHtml;
        if (messages.available === false) {
            messagesHtml = '<div class="compare-warn">至少一条日志没有完整请求消息，无法判断共享前缀。</div>';
        } else if (!first) {
            messagesHtml = `<div class="compare-ok">✅ 公共部分的 ${escapeHtml(String(messages.common_count ?? 0))} 条消息序列化内容一致。` +
                (messages.strict_append
                    ? `新请求保留旧请求的完整消息前缀（多出 ${escapeHtml(String((messages.appended || []).length))} 条）。`
                    : '新请求的消息历史比旧请求更短。') + '</div>';
        } else {
            messagesHtml = `<div class="compare-bad">💥 首个差异：<strong>message[${escapeHtml(String(first.index))}]</strong>（旧 ${escapeHtml(String(messages.old_count))} 条 / 新 ${escapeHtml(String(messages.new_count))} 条）` +
                '。此处可能影响共享前缀，实际缓存行为取决于上游。</div>' +
                `<div class="detail-item"><div class="detail-label">角色:</div><div class="detail-value">旧 <code>${escapeHtml(first.old_role ?? '—')}</code> → 新 <code>${escapeHtml(first.new_role ?? '—')}</code></div></div>` +
                `<div class="detail-item"><div class="detail-label">SHA:</div><div class="detail-value" style="font-family:monospace;font-size:12px;">旧 ${escapeHtml(first.old_sha256 ?? '')} → 新 ${escapeHtml(first.new_sha256 ?? '')}</div></div>` +
                `<div class="detail-item"><div class="detail-label">字符偏移:</div><div class="detail-value">canonical JSON 第 ${escapeHtml(String(first.canonical_char_offset ?? '—'))} 个字符（⇄ 为差异位置）</div></div>` +
                '<div class="compare-cols">' +
                `<div><div class="compare-col-title">旧 (A)</div><pre class="compare-context">${renderDiffContext(first.old_context, first.canonical_char_offset)}</pre></div>` +
                `<div><div class="compare-col-title">新 (B)</div><pre class="compare-context">${renderDiffContext(first.new_context, first.canonical_char_offset)}</pre></div>` +
                '</div>';
        }
        if (messages.strict_append && (messages.appended || []).length) {
            messagesHtml += '<details class="data-disclosure"><summary>追加的消息 <span>' + messages.appended.length + ' 条</span></summary><pre>' +
                escapeHtml(messages.appended.map(m => `[${m.index}] role=${m.role} name=${m.name} chars=${m.chars} sha=${m.sha256}`).join('\n')) + '</pre></details>';
        }

        let toolsHtml;
        if (tools.status === 'identical') {
            toolsHtml = `<div class="compare-ok">✅ 工具定义一致（${escapeHtml(String(tools.old_count ?? 0))} 个，sha ${escapeHtml(tools.old_sha256 ?? '')}）。</div>`;
        } else if (tools.status === 'different') {
            toolsHtml = '<div class="compare-bad">工具定义发生变化，可能影响上游构建的共享前缀。</div>' +
                `<div class="detail-item"><div class="detail-label">数量:</div><div class="detail-value">旧 ${escapeHtml(String(tools.old_count ?? '—'))} → 新 ${escapeHtml(String(tools.new_count ?? '—'))}</div></div>`;
            if (tools.first_difference) {
                toolsHtml += `<div class="detail-item"><div class="detail-label">首个差异:</div><div class="detail-value">tool[${escapeHtml(String(tools.first_difference.index))}] 旧 <code>${escapeHtml(tools.first_difference.old_name ?? '—')}</code> → 新 <code>${escapeHtml(tools.first_difference.new_name ?? '—')}</code></div></div>`;
            }
            if ((tools.removed || []).length || (tools.added || []).length) {
                toolsHtml += `<div class="detail-item"><div class="detail-label">增删:</div><div class="detail-value">移除 ${escapeHtml((tools.removed || []).join(', ') || '无')}；新增 ${escapeHtml((tools.added || []).join(', ') || '无')}${tools.same_name_set ? '（同名集合，顺序或 schema 变化）' : ''}</div></div>`;
            }
        } else {
            toolsHtml = '<div class="compare-warn">工具定义记录不完整，无法确认两次请求的工具是否相同。</div>';
        }

        let paramsHtml;
        if (params.same) {
            paramsHtml = '<div class="compare-ok">✅ 其余已记录的请求参数一致。</div>';
        } else {
            paramsHtml = '<div class="compare-bad">💥 以下已记录的请求参数发生变化：</div><div class="table-container"><table class="compare-table"><thead><tr><th>参数</th><th>旧 (A)</th><th>新 (B)</th></tr></thead><tbody>' +
                (params.differences || []).map(d =>
                    `<tr><td style="font-family:monospace;">${escapeHtml(d.key)}</td><td><pre>${escapeHtml(d.old ?? '')}</pre></td><td><pre>${escapeHtml(d.new ?? '')}</pre></td></tr>`
                ).join('') + '</tbody></table></div>';
        }

        const inferenceHtml = ((data.inference || []).length
            ? (data.inference || []).map(line => `<li>${escapeHtml(line)}</li>`).join('')
            : '<li>无结论</li>');

        let identityHtml;
        const identityFields = identity.fields || [];
        if (!identityFields.length) {
            identityHtml = '<div class="compare-warn">两条日志均未记录会话/调用方归属字段。</div>';
        } else if (identity.same) {
            identityHtml = `<div class="compare-ok">✅ 同一归属（${escapeHtml(identityFields.map(f => `${f.key}=${f.old}`).join(' · ') || '—')}）。归属相同不能证明上游缓存空间相同。</div>`;
        } else {
            identityHtml = '<div class="compare-warn">归属字段不同。网关归属与上游缓存路由并不等同，需结合上游配置判断。</div><div class="table-container"><table class="compare-table"><thead><tr><th>归属字段</th><th>旧 (A)</th><th>新 (B)</th></tr></thead><tbody>' +
                identityFields.map(f =>
                    `<tr><td style="font-family:monospace;">${escapeHtml(f.key)}</td><td><pre>${escapeHtml(f.old ?? '')}</pre></td><td><pre>${escapeHtml(f.new ?? '')}</pre></td></tr>`
                ).join('') + '</tbody></table></div>';
        }

        body.innerHTML = `
            ${modelWarn}
            <div class="detail-section compare-conclusion"><h3>分析结论</h3><ul class="compare-inference">${inferenceHtml}</ul></div>
            <div class="detail-section">
                <h3>缓存摘要${delta < 0 ? '（命中下降 ' + escapeHtml(String(-delta)) + '）' : delta > 0 ? '（命中上升 +' + escapeHtml(String(delta)) + '）' : '（持平）'}</h3>
                <div class="compare-cols">
                    <div><div class="compare-col-title">旧 (A) · ${escapeHtml(shortReqId(oldRef.request_id))}</div>
                        <div class="compare-nums"><div><span>模型</span><strong style="font-size:13px;">${escapeHtml(oldRef.model ?? '—')}</strong></div>
                        <div><span>时间</span><strong style="font-size:13px;">${escapeHtml(formatCompareTime(oldRef.timestamp))}</strong></div>
                        <div><span>输入</span><strong>${escapeHtml(formatCacheNum(oldSum.input_tokens))}</strong></div>
                        <div><span>缓存命中</span><strong>${escapeHtml(formatCacheNum(oldSum.cached_tokens))}</strong></div>
                        <div><span>命中率</span><strong>${escapeHtml(String(oldSum.hit_rate ?? '—'))}%</strong></div></div></div>
                    <div><div class="compare-col-title">新 (B) · ${escapeHtml(shortReqId(newRef.request_id))}</div>
                        <div class="compare-nums"><div><span>模型</span><strong style="font-size:13px;">${escapeHtml(newRef.model ?? '—')}</strong></div>
                        <div><span>时间</span><strong style="font-size:13px;">${escapeHtml(formatCompareTime(newRef.timestamp))}</strong></div>
                        <div><span>输入</span><strong>${escapeHtml(formatCacheNum(newSum.input_tokens))}</strong></div>
                        <div><span>缓存命中</span><strong>${escapeHtml(formatCacheNum(newSum.cached_tokens))}</strong></div>
                        <div><span>命中率</span><strong>${escapeHtml(String(newSum.hit_rate ?? '—'))}%</strong></div></div></div>
                </div>
                <div class="detail-item"><div class="detail-label">对齐分析:</div><div class="detail-value">两命中数 GCD=${escapeHtml(String(summary.cache_gcd ?? '—'))}${summary.both_divisible_by_128 ? '，均为 128 的倍数（仅为数值特征）' : '，不全是 128 的倍数'}${summary.start_to_start_gap_s != null ? `；开始间隔 ${escapeHtml(String(summary.start_to_start_gap_s))}s` : ''}${summary.prev_end_to_new_start_gap_s != null ? `；上条结束到本条开始 ${escapeHtml(String(summary.prev_end_to_new_start_gap_s))}s` : ''}</div></div>
                ${renderTpsMini('旧 (A)', tps.old)}${renderTpsMini('新 (B)', tps.new)}
            </div>
            <div class="detail-section"><h3>归属（会话/调用方）</h3>${identityHtml}</div>
            <div class="detail-section"><h3>请求消息（${escapeHtml(String(messages.old_count ?? 0))} → ${escapeHtml(String(messages.new_count ?? 0))} 条）</h3>${messagesHtml}</div>
            <div class="detail-section"><h3>工具定义</h3>${toolsHtml}</div>
            <div class="detail-section"><h3>请求参数</h3>${paramsHtml}</div>
        `;
    }

    return { computeTps, renderTpsRowHtml, updateCompareButton, toggleCompareSelection, toggleCompareSelectAll, clearCompareSelection, compareWithCurrent, openCompareSelected, openCompareModal, closeCompareModal,
        has: id => compareSelection.includes(id), get count() { return compareSelection.length; } };
})();
