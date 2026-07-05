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
                    const isDirectAPI = cfg.api_type === 'direct_api' || cfg.api_type === 'gemini_native' || cfg.api_type === 'anthropic_native';
                    const autoRetryConfig = (cfg && typeof cfg === 'object') ? (cfg.auto_retry || {}) : {};
                    const autoRetryEnabled = !!autoRetryConfig.enabled;
                    
                    let configInfo = '';
                    let modeInfo = '';
                    
                    if (isDirectAPI) {
                        const baseUrl = cfg.api_base_url || '';
                        const displayUrl = baseUrl.length > 30 ? baseUrl.substring(0, 30) + '...' : baseUrl;
                        const apiTypeLabel = cfg.api_type === 'gemini_native' ? 'Gemini原生' : (cfg.api_type === 'anthropic_native' ? 'Anthropic原生' : 'OpenAI兼容');
                        const endpointPath = (cfg.endpoint_path || '/chat/completions');
                        
                        // 🔧 多 API Key 轮询：显示 key 数量
                        let apiKeyDisplay = '';
                        if (cfg.api_keys && Array.isArray(cfg.api_keys) && cfg.api_keys.length > 0) {
                            apiKeyDisplay = `<div><strong>Key:</strong> ${cfg.api_keys.length} 个（轮询）</div>`;
                        } else if (cfg.api_key) {
                            const keyPreview = typeof cfg.api_key === 'string' && cfg.api_key.length > 10
                                ? cfg.api_key.substring(0, 4) + '...' + cfg.api_key.substring(cfg.api_key.length - 4)
                                : '***';
                            apiKeyDisplay = `<div><strong>Key:</strong> ${keyPreview}</div>`;
                        } else {
                            apiKeyDisplay = `<div><strong>Key:</strong> 无需认证</div>`;
                        }
                        
                        configInfo = `
                            <div style="font-size: 0.875rem;">
                                <div><strong>类型:</strong> ${apiTypeLabel}</div>
                                <div><strong>URL:</strong> ${displayUrl || '(默认)'}</div>
                                <div><strong>端点:</strong> ${endpointPath}</div>
                                <div><strong>模型:</strong> ${cfg.model_id || name}</div>
                                ${apiKeyDisplay}
                                <div><strong>重试:</strong> ${autoRetryEnabled ? `开启（${autoRetryConfig.max_retries ?? 2}次, ${autoRetryConfig.retry_delay_seconds ?? 2}s）` : '关闭'}</div>
                                ${cfg.pricing ? `<div><strong>计费:</strong> ${cfg.pricing.input}/${cfg.pricing.output} ${cfg.pricing.currency}</div>` : ''}
                            </div>
                        `;
                        modeInfo = `
                            <span class="badge badge-success">Direct API</span>
                            ${cfg.passthrough ? '<span class="badge badge-info">透传</span>' : ''}
                            ${cfg.api_type === 'anthropic_native' ? '<span class="badge badge-info">Anthropic原生</span>' : ''}
                            ${cfg.api_type === 'gemini_native' ? '<span class="badge badge-info">Gemini原生</span>' : ''}
                            ${autoRetryEnabled ? '<span class="badge" style="background: rgba(249, 115, 22, 0.2); color: #f97316; border-color: rgba(249, 115, 22, 0.3);">🔁重试</span>' : ''}
                            ${cfg.image_compression?.enabled ? '<span class="badge" style="background: rgba(147, 51, 234, 0.2); color: #a855f7; border-color: rgba(147, 51, 234, 0.3);">🖼️压缩</span>' : ''}
                        `;
                } else {
                    configInfo = `
                        <div style="font-size: 0.875rem;">
                            <div><strong>Session:</strong> <code style="color: var(--accent);">...${cfg.session_id?.slice(-8) || 'N/A'}</code></div>
                            <div><strong>状态:</strong> <span style="color: #f59e0b;">已弃用，保留兼容</span></div>
                            ${cfg.type ? `<div><strong>类型:</strong> ${cfg.type}</div>` : ''}
                        </div>
                    `;
                    modeInfo = `
                        <span class="badge" style="background: rgba(245, 158, 11, 0.2); color: #f59e0b; border-color: rgba(245, 158, 11, 0.3);">已弃用</span>
                        <span class="badge badge-info">${cfg.mode || 'direct_chat'}</span>
                        ${cfg.battle_target ? `<span class="badge badge-info">${cfg.battle_target}</span>` : ''}
                    `;
                }
                
                return `
                    <tr class="model-row" data-model-name="${name}">
                        <td class="drag-handle" title="拖动排序">⠿</td>
                        <td><strong>${name}</strong></td>
                        <td>
                            <span class="badge ${isDirectAPI ? 'badge-success' : 'badge-info'}">
                                ${isDirectAPI ? 'API' : 'LMArena（已弃用）'}
                            </span>
                        </td>
                        <td>${configInfo}</td>
                        <td>${modeInfo}</td>
                        <td>
                            <button class="btn btn-primary btn-sm" onclick='editModel("${name}", ${JSON.stringify(cfg).replace(/'/g, "\\'")} )'>编辑</button>
                            <button class="btn btn-sm" onclick='copyModel("${name}", ${JSON.stringify(cfg).replace(/'/g, "\\'")} )' title="复制此模型配置">📋 复制</button>
                            <button class="btn btn-danger btn-sm" onclick='deleteModel("${name}")'>删除</button>
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
// 简单 HTML 转义
function escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.appendChild(document.createTextNode(text));
    return div.innerHTML;
}