// admin-models-list.js - 模型列表渲染、排序、删除
'use strict';

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
        
    } catch (error) {
        console.error('❌ 加载模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '加载模型失败: ' + error.message);
    }
}

function buildModelsTable(modelEndpointMap) {
    return `<table class="table">
        <thead>
            <tr>
                <th style="width: 40px;"></th>
                <th>模型名称</th>
                <th>类型</th>
                <th>配置信息</th>
                <th>模式</th>
                <th>操作</th>
            </tr>
        </thead>
        <tbody id="models-tbody">
            ${Object.entries(modelEndpointMap).map(([name, config]) => {
                const cfg = Array.isArray(config) ? config[0] : config;
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
                return `
                    <tr class="model-row" data-model-name="${nameAttr}">
                        <td class="drag-handle" title="拖动排序">⠿</td>
                        <td><strong>${escapeHtml(name)}</strong></td>
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
                            <button class="btn btn-danger btn-sm" data-model-name="${nameAttr}"
                                onclick="deleteModel(this.dataset.modelName)">删除</button>
                        </td>
                    </tr>
                `;
            }).join('')}
        </tbody>
    </table>`;
}

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

async function testAllApiKeys() {
    const container = document.getElementById('test-results-container');
    const summaryEl = document.getElementById('test-results-summary');
    const tbody = document.getElementById('test-results-body');
    const btn = document.getElementById('test-all-keys-btn');
    
    if (!container || !tbody) return;
    
    // 显示加载状态
    container.style.display = 'block';
    summaryEl.innerHTML = '<span style="color: var(--text-dim);">⏳ 正在测试所有 API Key...</span>';
    tbody.innerHTML = '';
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 测试中...'; }
    
    try {
        const resp = await fetch('/api/admin/models');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        const modelMap = data.model_endpoint_map || {};
        
        // 筛选有 api_keys 的 direct_api 模型
        const candidates = [];
        for (const [name, config] of Object.entries(modelMap)) {
            const cfg = Array.isArray(config) ? config[0] : config;
            if (!cfg || typeof cfg !== 'object') continue;
            const apiType = cfg.api_type || '';
            if (!['direct_api', 'responses_native', 'gemini_native', 'anthropic_native'].includes(apiType)) continue;
            const keys = cfg.api_keys || (cfg.api_key ? [cfg.api_key] : []);
            if (!keys.length) continue;
            candidates.push({
                model_name: name,
                api_keys: keys,
                api_base_url: cfg.api_base_url || '',
                model_id: cfg.model_id || name,
                api_type: apiType,
                endpoint_path: cfg.endpoint_path || (
                    apiType === 'responses_native' ? '/responses' : (
                        apiType === 'anthropic_native' ? '/messages' : '/chat/completions'
                    )
                )
            });
        }
        
        if (candidates.length === 0) {
            summaryEl.innerHTML = '<span style="color: var(--text-dim);">没有找到可测试的模型（需配置 api_keys 的 direct_api 模型）</span>';
            if (btn) { btn.disabled = false; btn.textContent = '🔑 一键测试所有 Key'; }
            return;
        }
        
        // 逐个测试
        let passed = 0, failed = 0;
        for (const c of candidates) {
            const row = document.createElement('tr');
            row.innerHTML = `<td>${escapeHtml(c.model_name)}</td><td>${escapeHtml(c.model_id)}</td><td>⏳ 测试中...</td><td>-</td><td>-</td>`;
            tbody.appendChild(row);
            
            try {
                const testResp = await fetch('/api/admin/test_model_keys', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        api_keys: c.api_keys,
                        api_base_url: c.api_base_url,
                        model_id: c.model_id,
                        api_type: c.api_type,
                        endpoint_path: c.endpoint_path
                    })
                });
                const result = await testResp.json();
                const ok = testResp.ok && result.status === 'success';
                if (ok) passed++; else failed++;
                const statusBadge = ok
                    ? '<span class="badge badge-success">✓ 通过</span>'
                    : '<span class="badge badge-danger">✗ 失败</span>';
                const detail = result.message || result.detail || JSON.stringify(result);
                row.innerHTML = `<td>${escapeHtml(c.model_name)}</td><td>${escapeHtml(c.model_id)}</td><td>${statusBadge}</td><td>${result.response_time_ms != null ? result.response_time_ms + 'ms' : '-'}</td><td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(detail)}">${escapeHtml(detail)}</td>`;
            } catch (e) {
                failed++;
                row.innerHTML = `<td>${escapeHtml(c.model_name)}</td><td>${escapeHtml(c.model_id)}</td><td><span class="badge badge-danger">✗ 异常</span></td><td>-</td><td>${escapeHtml(e.message)}</td>`;
            }
        }
        
        summaryEl.innerHTML = `<strong>测试完成：</strong>
            <span class="badge badge-success">✓ ${passed} 通过</span>
            <span class="badge badge-danger">✗ ${failed} 失败</span>
            （共 ${candidates.length} 个模型）`;
    } catch (error) {
        summaryEl.innerHTML = `<span style="color: var(--danger);">测试失败: ${escapeHtml(error.message)}</span>`;
        console.error('testAllApiKeys 失败:', error);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔑 一键测试所有 Key'; }
    }
}

// escapeHtml 统一使用 admin-core.js 的全局实现（转义 &<>"'，属性上下文也安全）。
// 旧版这里用 createTextNode+innerHTML 重复定义，不转义引号且会覆盖正确实现。