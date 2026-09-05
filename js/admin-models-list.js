// admin-models-list.js - 模型列表渲染、排序、删除、归档
'use strict';

// ==================== 归档辅助 ====================

function isModelConfigArchived(config) {
    // 与后端 core/model_archive.is_model_archived 语义一致：dict 或 list 取第一个
    if (Array.isArray(config)) {
        const first = (config.length && typeof config[0] === 'object' && config[0] !== null) ? config[0] : {};
        return !!first.archived;
    }
    if (config && typeof config === 'object') {
        return !!config.archived;
    }
    return false;
}

function updateArchiveButtons() {
    const archiveBtn = document.getElementById('archive-selected-btn');
    const restoreBtn = document.getElementById('restore-selected-btn');
    const activeChecked = document.querySelectorAll('#models-tbody .model-row input[type="checkbox"]:checked').length;
    const archivedChecked = document.querySelectorAll('#archived-models-tbody .model-row input[type="checkbox"]:checked').length;
    if (archiveBtn) archiveBtn.disabled = activeChecked === 0;
    if (restoreBtn) restoreBtn.disabled = archivedChecked === 0;
    if (archiveBtn) archiveBtn.textContent = activeChecked ? `归档所选（${activeChecked}）` : '归档所选';
    for (const [tbodyId, headerId] of [['models-tbody', 'select-all-active'], ['archived-models-tbody', 'select-all-archived']]) {
        const header = document.getElementById(headerId);
        if (!header) continue;
        const boxes = Array.from(document.querySelectorAll(`#${tbodyId} .model-row:not([hidden]) .model-checkbox`));
        const selected = boxes.filter(box => box.checked).length;
        header.checked = boxes.length > 0 && selected === boxes.length;
        header.indeterminate = selected > 0 && selected < boxes.length;
        header.disabled = boxes.length === 0;
    }
}

// ==================== 列表加载 ====================

async function loadModels() {
    const request = ++modelWorkspace.request;
    document.getElementById('models-summary').textContent = '正在加载模型…';
    try {
        const response = await fetch('/api/admin/models');
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        const data = await response.json();
        if (request !== modelWorkspace.request) return;
        modelWorkspace.models = data.model_endpoint_map || {};
        refreshModelProviders();
        
        const modelsHtml = Object.keys(data.model_endpoint_map).length > 0
            ? buildModelsTable(data.model_endpoint_map)
            : '<div class="empty-state"><div class="empty-state-icon">🤖</div><p>还没有配置任何模型<br/>点击"添加模型"开始配置</p></div>';
        
        document.getElementById('models-list').innerHTML = modelsHtml;
        initModelsSortable();
        applyModelFilters();
        
    } catch (error) {
        if (request !== modelWorkspace.request) return;
        document.getElementById('models-summary').textContent = '加载失败，请点击刷新列表重试。';
        console.error('❌ 加载模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '加载模型失败: ' + error.message);
    }
}

function buildModelsRow(name, cfg, isArchived) {
    const protocol = modelProtocol(cfg);
    const labels = { direct_api: 'OpenAI 兼容', responses_native: 'Responses', anthropic_native: 'Anthropic', gemini_native: 'Gemini', legacy: 'LMArena · 已弃用' };
    const endpoint = cfg.endpoint_path || ({direct_api:'/chat/completions', responses_native:'/responses', anthropic_native:'/messages', gemini_native: cfg.upstream_protocol === 'interactions' ? '/v1beta/interactions' : 'generateContent'}[protocol] || '会话连接');
    const attr = escapeHtml(name);
    const keyCount = Array.isArray(cfg.api_keys) && cfg.api_keys.length ? cfg.api_keys.length : (cfg.api_key ? 1 : 0);
    const flags = [cfg.auto_retry?.enabled ? '自动重试' : '', cfg.image_compression?.enabled ? '图片压缩' : '', cfg.enable_thinking ? '思考配置' : '', cfg.passthrough && protocol === 'direct_api' ? '透传' : ''].filter(Boolean);
    return `<tr class="model-row${isArchived ? ' model-row-archived' : ''}" data-model-name="${attr}">
        <td><input type="checkbox" class="model-checkbox" onchange="updateArchiveButtons()" aria-label="选择 ${attr}"></td>
        <td class="drag-handle" title="拖动调整模型顺序">⠿</td>
        <td class="model-identity"><button class="model-name" data-model-name="${attr}" onclick="openModelFromList(this.dataset.modelName)">${attr}</button>
            <div class="model-subtitle">${escapeHtml(cfg.display_name && cfg.display_name !== name ? cfg.display_name : cfg.model_id || name)}</div>
            <div class="model-flags"><span class="badge ${isArchived ? 'badge-info' : 'badge-success'}">${isArchived ? '已归档' : '可用'}</span>${flags.map(flag => `<span class="badge badge-info">${flag}</span>`).join('')}</div></td>
        <td class="model-connection"><span class="badge badge-info">${labels[protocol]}</span>
            <div class="model-subtitle">${escapeHtml(modelService(cfg))}</div><div class="model-subtitle">${escapeHtml(endpoint)} · ${keyCount ? keyCount + ' 个 Key' : '无需认证'}</div></td>
        <td><div class="model-actions"><button class="btn btn-primary btn-sm" data-model-name="${attr}" onclick="openModelFromList(this.dataset.modelName)">设置</button>
            <details class="model-more"><summary class="btn btn-sm" aria-label="${attr} 的更多操作">更多</summary>
                <button class="btn btn-sm" data-model-name="${attr}" onclick="openModelFromList(this.dataset.modelName, true)">复制模型</button>
                <button class="btn btn-sm" data-model-name="${attr}" onclick="toggleModelArchive(this.dataset.modelName)">${isArchived ? '恢复模型' : '归档模型'}</button>
                <button class="btn btn-danger btn-sm" data-model-name="${attr}" onclick="deleteModel(this.dataset.modelName)">删除模型</button>
            </details></div></td></tr>`;
}

function buildModelsTable(modelEndpointMap) {
    const entries = Object.entries(modelEndpointMap);
    const activeEntries = [];
    const archivedEntries = [];
    for (const [name, config] of entries) {
        const cfg = Array.isArray(config) ? (config[0] || {}) : config;
        if (isModelConfigArchived(config)) {
            archivedEntries.push([name, cfg]);
        } else {
            activeEntries.push([name, cfg]);
        }
    }

    // 活跃模型表格
    let html = `<table class="table">
        <thead>
            <tr>
                <th style="width: 36px;"><input type="checkbox" id="select-all-active" title="全选活跃模型" onchange="toggleSelectAll(this, 'models-tbody')"></th>
                <th style="width: 40px;"></th>
                <th>模型名称</th>
                <th>上游连接</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody id="models-tbody">`;
    if (activeEntries.length === 0) {
        html += `<tr><td colspan="5" style="text-align: center; color: var(--text-dim); padding: 24px;">没有活跃模型${archivedEntries.length ? `（${archivedEntries.length} 个已归档）` : ''}</td></tr>`;
    } else {
        html += activeEntries.map(([name, cfg]) => buildModelsRow(name, cfg, false)).join('');
    }
    html += `</tbody></table>`;

    // 已归档模型折叠区
    if (archivedEntries.length > 0) {
        html += `
        <div class="archive-section" style="margin-top: 18px; border: 1px solid var(--line-strong); border-radius: 8px; overflow: hidden;">
            <button type="button" aria-expanded="false" class="archive-header" onclick="toggleArchiveSection()" style="display: flex; align-items: center; gap: 8px; padding: 10px 14px; cursor: pointer; background: var(--surface-2); user-select: none;">
                <span id="archive-toggle-icon">▶</span>
                <strong>📦 已归档模型（${archivedEntries.length}）</strong>
                <span style="color: var(--text-dim); font-size: 0.8rem;">点击展开/折叠 · 归档模型无法请求、不出现在模型列表</span>
            </button>
            <div id="archive-section-body" style="display: none; border-top: 1px solid var(--line-strong);">
                <div style="padding: 8px 14px; display: flex; gap: 10px; align-items: center; background: var(--surface-2);">
                    <button class="btn btn-sm" id="restore-selected-btn" disabled onclick="restoreSelectedModels()" title="恢复勾选的归档模型">♻️ 恢复所选</button>
                    <span style="color: var(--text-dim); font-size: 0.8rem;">勾选后点击恢复，模型重新可请求</span>
                </div>
                <table class="table">
                    <thead>
                        <tr>
                            <th style="width: 36px;"><input type="checkbox" id="select-all-archived" title="全选归档模型" onchange="toggleSelectAll(this, 'archived-models-tbody')"></th>
                            <th style="width: 40px;"></th>
                            <th>模型名称</th>
                            <th>上游连接</th>
                            <th>操作</th>
                        </tr>
                    </thead>
                    <tbody id="archived-models-tbody">
                        ${archivedEntries.map(([name, cfg]) => buildModelsRow(name, cfg, true)).join('')}
                    </tbody>
                </table>
            </div>
        </div>`;
    }

    return html;
}

// ==================== 归档区折叠 ====================

function toggleArchiveSection() {
    const body = document.getElementById('archive-section-body');
    const icon = document.getElementById('archive-toggle-icon');
    if (!body || !icon) return;
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? 'block' : 'none';
    icon.textContent = collapsed ? '▼' : '▶';
    document.querySelector('.archive-header').setAttribute('aria-expanded', String(collapsed));
}

function toggleSelectAll(checkbox, tbodyId) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    tbody.querySelectorAll('.model-row:not([hidden]) input[type="checkbox"]').forEach(cb => { cb.checked = checkbox.checked; });
    updateArchiveButtons();
}

// ==================== 排序 ====================

function initModelsSortable() {
    const tbody = document.getElementById('models-tbody');
    if (!tbody) return;
    
    if (modelsSortable) modelsSortable.destroy();
    
    modelsSortable = new Sortable(tbody, {
        animation: 150,
        handle: '.drag-handle',
        ghostClass: 'sortable-ghost',
        dragClass: 'sortable-drag',
        onStart: function(evt) { evt.item.classList.add('sorting'); },
        onEnd: function(evt) {
            evt.item.classList.remove('sorting');
            saveModelsOrder();
        }
    });
}

async function saveModelsOrder() {
    try {
        const tbody = document.getElementById('models-tbody');
        if (!tbody) return;
        
        // 只提交活跃区顺序（归档区不参与拖拽；后端会把未提交的模型追加到末尾）
        const rows = tbody.querySelectorAll('.model-row');
        const newOrder = Array.from(rows).map(row => row.getAttribute('data-model-name'));
        
        const response = await fetch('/api/admin/models/reorder', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ order: newOrder })
        });
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        showQuietMessage('success', '✓ 顺序已保存');
        
    } catch (error) {
        console.error('❌ 保存模型顺序失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '保存顺序失败: ' + error.message);
    }
}

// ==================== 归档 / 恢复 ====================

async function setModelsArchived(modelNames, archived) {
    try {
        const response = await fetch('/api/admin/models/archive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_names: modelNames, archived: archived })
        });

        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }

        const result = await response.json();
        loadModels();
        showMessage('success', result.message || (archived ? '归档成功' : '恢复成功'));
        return result;
    } catch (error) {
        console.error('归档操作失败:', error);
        showMessage('danger', (archived ? '归档失败: ' : '恢复失败: ') + error.message);
    }
}

function toggleModelArchive(name) {
    // 从当前渲染的配置判断状态（行内按钮只带模型名，重新拉一次配置最稳妥）
    const row = document.querySelector(`.model-row[data-model-name="${CSS.escape(name)}"]`);
    const isArchived = row ? row.classList.contains('model-row-archived') : false;
    if (!confirm(`确定要${isArchived ? '恢复' : '归档'}模型 "${name}" 吗？\n\n${isArchived ? '恢复后模型可重新请求、出现在模型列表。' : '归档后模型无法请求、从模型列表隐藏，可在"已归档模型"区恢复。'}`)) return;
    setModelsArchived([name], !isArchived);
}

function archiveSelectedModels() {
    const checked = Array.from(document.querySelectorAll('#models-tbody .model-row input[type="checkbox"]:checked'))
        .map(cb => cb.closest('.model-row').getAttribute('data-model-name'));
    if (checked.length === 0) return;
    if (!confirm(`确定要归档选中的 ${checked.length} 个模型吗？\n\n归档后模型无法请求、从模型列表隐藏，可在"已归档模型"区恢复。`)) return;
    setModelsArchived(checked, true);
}

function restoreSelectedModels() {
    const checked = Array.from(document.querySelectorAll('#archived-models-tbody .model-row input[type="checkbox"]:checked'))
        .map(cb => cb.closest('.model-row').getAttribute('data-model-name'));
    if (checked.length === 0) return;
    if (!confirm(`确定要恢复选中的 ${checked.length} 个模型吗？`)) return;
    setModelsArchived(checked, false);
}

// ==================== 自动归档 ====================

async function runAutoArchive(forceDays) {
    // 天数优先取调用方显式传入的值（设置弹窗内“不保存立即扫描”用输入框值）；
    // 否则读后端已保存的配置，避免页面刷新后输入框回到默认值导致扫描阈值错乱
    let days = forceDays;
    if (days === undefined) {
        days = 30;
        try {
            const resp = await fetch('/api/admin/models/archive_config');
            if (resp.ok) {
                const cfg = await resp.json();
                days = parseInt(cfg.days, 10) || 30;
            }
        } catch (e) {
            console.warn('读取自动归档配置失败，使用默认 30 天:', e);
        }
    }
    if (!confirm(`将扫描所有模型，归档超过 ${days} 天未调用的模型（无调用记录的新模型不受影响）。确定继续？`)) return;

    try {
        const response = await fetch('/api/admin/models/auto_archive', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ days: days })
        });
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        const result = await response.json();
        loadModels();
        const archivedNames = (result.archived || []);
        let detail = '';
        if (archivedNames.length > 0) {
            const preview = archivedNames.slice(0, 8).join('、') + (archivedNames.length > 8 ? ` 等共 ${archivedNames.length} 个` : '');
            detail = '（' + preview + '）';
        }
        showMessage('success', (result.message || '扫描完成') + detail);
    } catch (error) {
        console.error('自动归档失败:', error);
        showMessage('danger', '自动归档失败: ' + error.message);
    }
}

async function loadArchiveSettings() {
    try {
        const response = await fetch('/api/admin/models/archive_config');
        if (!response.ok) return;
        const cfg = await response.json();
        const enabledEl = document.getElementById('archive-auto-enabled');
        const daysEl = document.getElementById('archive-auto-days');
        if (enabledEl) enabledEl.checked = !!cfg.enabled;
        if (daysEl) daysEl.value = cfg.days || 30;
    } catch (error) {
        console.error('加载自动归档配置失败:', error);
    }
}

function showArchiveSettings() {
    loadArchiveSettings();
    const modal = document.getElementById('archive-settings-modal');
    if (modal) modal.style.display = 'flex';
}

function closeArchiveSettings() {
    const modal = document.getElementById('archive-settings-modal');
    if (modal) modal.style.display = 'none';
}

async function saveArchiveSettings() {
    const enabled = document.getElementById('archive-auto-enabled').checked;
    const days = parseInt(document.getElementById('archive-auto-days').value || '30', 10);
    if (!(days >= 1)) {
        alert('天数必须为正整数');
        return;
    }
    try {
        const response = await fetch('/api/admin/models/archive_config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled, days: days })
        });
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        const result = await response.json();
        closeArchiveSettings();
        loadModels();
        showMessage('success', result.message || '自动归档设置已保存');
    } catch (error) {
        console.error('保存自动归档配置失败:', error);
        showMessage('danger', '保存自动归档配置失败: ' + error.message);
    }
}

// ==================== 模态框操作 ====================
function toggleConfigType() {
    const configType = document.getElementById('config-type').value;
    document.getElementById('lmarena-config').style.display = configType === 'direct_api' ? 'none' : 'block';
    syncModelSettingsType();
}

function toggleBattleTarget() {
    const mode = document.getElementById('mode').value;
    document.getElementById('battle-target-group').style.display = mode === 'battle' ? 'block' : 'none';
}
async function deleteModel(name) {
    if (!confirm(`确定要删除模型 "${name}" 吗？`)) return;
    
    try {
        const response = await fetch('/api/admin/models/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_name: name })
        });
        
        if (!response.ok) {
            const errorText = await response.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${response.status}): ${errorDetail}`);
        }
        
        loadModels();
        showMessage('success', `模型 ${name} 已删除`);
        
    } catch (error) {
        console.error('删除模型失败:', error);
        alert('删除失败: ' + error.message);
    }
}

// escapeHtml 统一使用 admin-core.js 的全局实现（转义 &<>"'，属性上下文也安全）。
// 旧版这里用 createTextNode+innerHTML 重复定义，不转义引号且会覆盖正确实现。
