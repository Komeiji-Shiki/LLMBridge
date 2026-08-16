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
}

// ==================== 列表加载 ====================

async function loadModels() {
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
        
        const modelsHtml = Object.keys(data.model_endpoint_map).length > 0
            ? buildModelsTable(data.model_endpoint_map)
            : '<div class="empty-state"><div class="empty-state-icon">🤖</div><p>还没有配置任何模型<br/>点击"添加模型"开始配置</p></div>';
        
        document.getElementById('models-list').innerHTML = modelsHtml;
        initModelsSortable();
        updateArchiveButtons();
        
    } catch (error) {
        console.error('❌ 加载模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '加载模型失败: ' + error.message);
    }
}

function buildModelsRow(name, cfg, isArchived) {
    const isDirectAPI = ['direct_api', 'responses_native', 'gemini_native', 'anthropic_native'].includes(cfg.api_type);
    const autoRetryConfig = (cfg && typeof cfg === 'object') ? (cfg.auto_retry || {}) : {};
    const autoRetryEnabled = !!autoRetryConfig.enabled;

    let configInfo = '';
    let modeInfo = '';

    if (isDirectAPI) {
        const baseUrl = cfg.api_base_url || '';
        const displayUrl = escapeHtml(baseUrl.length > 30 ? baseUrl.substring(0, 30) + '...' : baseUrl);
        const apiTypeLabels = {
            direct_api: 'OpenAI兼容',
            responses_native: 'Responses原生',
            gemini_native: 'Gemini原生',
            anthropic_native: 'Anthropic原生',
        };
        const apiTypeLabel = apiTypeLabels[cfg.api_type] || 'OpenAI兼容';
        const defaultEndpoint = cfg.api_type === 'responses_native'
            ? '/responses'
            : (cfg.api_type === 'anthropic_native' ? '/messages' : '/chat/completions');
        const endpointPath = escapeHtml(cfg.endpoint_path || defaultEndpoint);

        // 🔧 多 API Key 轮询：显示 key 数量
        let apiKeyDisplay = '';
        if (cfg.api_keys && Array.isArray(cfg.api_keys) && cfg.api_keys.length > 0) {
            apiKeyDisplay = `<div><strong>Key:</strong> ${cfg.api_keys.length} 个（轮询）</div>`;
        } else if (cfg.api_key) {
            const keyPreview = typeof cfg.api_key === 'string' && cfg.api_key.length > 10
                ? cfg.api_key.substring(0, 4) + '...' + cfg.api_key.substring(cfg.api_key.length - 4)
                : '***';
            apiKeyDisplay = `<div><strong>Key:</strong> ${escapeHtml(keyPreview)}</div>`;
        } else {
            apiKeyDisplay = `<div><strong>Key:</strong> 无需认证</div>`;
        }

        configInfo = `
            <div style="font-size: 0.875rem;">
                <div><strong>类型:</strong> ${apiTypeLabel}</div>
                <div><strong>URL:</strong> ${displayUrl || '(默认)'}</div>
                <div><strong>端点:</strong> ${endpointPath}</div>
                <div><strong>模型:</strong> ${escapeHtml(cfg.model_id || name)}</div>
                ${apiKeyDisplay}
                <div><strong>重试:</strong> ${autoRetryEnabled ? `开启（${autoRetryConfig.max_retries ?? 2}次, ${autoRetryConfig.retry_delay_seconds ?? 2}s）` : '关闭'}</div>
                ${cfg.pricing ? `<div><strong>计费:</strong> ${escapeHtml(String(cfg.pricing.input))}/${escapeHtml(String(cfg.pricing.output))} ${escapeHtml(cfg.pricing.currency || '')}</div>` : ''}
            </div>
        `;
        modeInfo = `
            <span class="badge badge-success">Direct API</span>
            ${cfg.api_type === 'direct_api' && cfg.passthrough ? '<span class="badge badge-info">透传</span>' : ''}
            ${cfg.api_type === 'responses_native' ? '<span class="badge badge-info">Responses原生</span>' : ''}
            ${cfg.api_type === 'anthropic_native' ? '<span class="badge badge-info">Anthropic原生</span>' : ''}
            ${cfg.api_type === 'gemini_native' ? '<span class="badge badge-info">Gemini原生</span>' : ''}
            ${autoRetryEnabled ? '<span class="badge" style="background: rgba(249, 115, 22, 0.2); color: #f97316; border-color: rgba(249, 115, 22, 0.3);">🔁重试</span>' : ''}
            ${cfg.image_compression?.enabled ? '<span class="badge" style="background: rgba(147, 51, 234, 0.2); color: #a855f7; border-color: rgba(147, 51, 234, 0.3);">🖼️压缩</span>' : ''}
        `;
    } else {
        configInfo = `
            <div style="font-size: 0.875rem;">
                <div><strong>Session:</strong> <code style="color: var(--accent);">...${escapeHtml(cfg.session_id?.slice(-8) || 'N/A')}</code></div>
                <div><strong>状态:</strong> <span style="color: #f59e0b;">已弃用，保留兼容</span></div>
                ${cfg.type ? `<div><strong>类型:</strong> ${escapeHtml(cfg.type)}</div>` : ''}
            </div>
        `;
        modeInfo = `
            <span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #f59e0b; border-color: rgba(245, 158, 11, 0.3);">已弃用</span>
            <span class="badge badge-info">${escapeHtml(cfg.mode || 'direct_chat')}</span>
            ${cfg.battle_target ? `<span class="badge badge-info">${escapeHtml(cfg.battle_target)}</span>` : ''}
        `;
    }

    // 配置 JSON 放入 data 属性（escapeHtml 转义后安全），点击时 JSON.parse 还原；
    // 旧版把 JSON 直接拼进 onclick 单引号属性，HTML 不认反斜杠转义，
    // 配置值含引号/尖括号时会打破属性边界
    const nameAttr = escapeHtml(name);
    const cfgAttr = escapeHtml(JSON.stringify(cfg));
    const archiveAction = isArchived
        ? `<button class="btn btn-sm" data-model-name="${nameAttr}" onclick="toggleModelArchive(this.dataset.modelName)" title="恢复后模型重新可请求、出现在模型列表">♻️ 恢复</button>`
        : `<button class="btn btn-sm" data-model-name="${nameAttr}" onclick="toggleModelArchive(this.dataset.modelName)" title="归档后模型无法请求、从模型列表隐藏">🗄️ 归档</button>`;

    return `
        <tr class="model-row${isArchived ? ' model-row-archived' : ''}" data-model-name="${nameAttr}">
            <td style="width: 36px;"><input type="checkbox" class="model-checkbox" onchange="updateArchiveButtons()" title="选择此模型"></td>
            <td class="drag-handle" title="拖动排序">⠿</td>
            <td><strong>${escapeHtml(name)}</strong>${isArchived ? ' <span class="badge" style="background: rgba(107, 114, 128, 0.2); color: #9ca3af; border-color: rgba(107, 114, 128, 0.3);">已归档</span>' : ''}</td>
            <td>
                <span class="badge ${isDirectAPI ? 'badge-success' : 'badge-info'}">
                    ${isDirectAPI ? 'API' : 'LMArena（已弃用）'}
                </span>
            </td>
            <td>${configInfo}</td>
            <td>${modeInfo}</td>
            <td>
                <button class="btn btn-primary btn-sm" data-model-name="${nameAttr}" data-model-config="${cfgAttr}"
                    onclick="editModel(this.dataset.modelName, JSON.parse(this.dataset.modelConfig))">编辑</button>
                <button class="btn btn-sm" data-model-name="${nameAttr}" data-model-config="${cfgAttr}"
                    onclick="copyModel(this.dataset.modelName, JSON.parse(this.dataset.modelConfig))" title="复制此模型配置">📋 复制</button>
                ${archiveAction}
                <button class="btn btn-danger btn-sm" data-model-name="${nameAttr}"
                    onclick="deleteModel(this.dataset.modelName)">删除</button>
            </td>
        </tr>
    `;
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
                <th>类型</th>
                <th>配置信息</th>
                <th>模式</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody id="models-tbody">`;
    if (activeEntries.length === 0) {
        html += `<tr><td colspan="7" style="text-align: center; color: var(--text-dim); padding: 24px;">没有活跃模型${archivedEntries.length ? `（${archivedEntries.length} 个已归档）` : ''}</td></tr>`;
    } else {
        html += activeEntries.map(([name, cfg]) => buildModelsRow(name, cfg, false)).join('');
    }
    html += `</tbody></table>`;

    // 已归档模型折叠区
    if (archivedEntries.length > 0) {
        html += `
        <div class="archive-section" style="margin-top: 18px; border: 1px solid var(--line-strong); border-radius: 8px; overflow: hidden;">
            <div class="archive-header" onclick="toggleArchiveSection()" style="display: flex; align-items: center; gap: 8px; padding: 10px 14px; cursor: pointer; background: var(--surface-2); user-select: none;">
                <span id="archive-toggle-icon">▶</span>
                <strong>📦 已归档模型（${archivedEntries.length}）</strong>
                <span style="color: var(--text-dim); font-size: 0.8rem;">点击展开/折叠 · 归档模型无法请求、不出现在模型列表</span>
            </div>
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
                            <th>类型</th>
                            <th>配置信息</th>
                            <th>模式</th>
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
}

function toggleSelectAll(checkbox, tbodyId) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    tbody.querySelectorAll('input[type="checkbox"]').forEach(cb => { cb.checked = checkbox.checked; });
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
        throw error;
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
    document.getElementById('direct-api-config').style.display = configType === 'direct_api' ? 'block' : 'none';
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
