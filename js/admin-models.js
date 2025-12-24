// admin-models.js - 模型管理功能

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
                    const isDirectAPI = cfg.api_type === 'direct_api' || cfg.api_type === 'gemini_native';
                    
                    let configInfo = '';
                    let modeInfo = '';
                    
                    if (isDirectAPI) {
                        const baseUrl = cfg.api_base_url || '';
                        const displayUrl = baseUrl.length > 30 ? baseUrl.substring(0, 30) + '...' : baseUrl;
                        const apiTypeLabel = cfg.api_type === 'gemini_native' ? 'Gemini原生' : 'OpenAI兼容';
                        configInfo = `
                            <div style="font-size: 0.875rem;">
                                <div><strong>类型:</strong> ${apiTypeLabel}</div>
                                <div><strong>URL:</strong> ${displayUrl || '(默认)'}</div>
                                <div><strong>模型:</strong> ${cfg.model_id || name}</div>
                                ${cfg.pricing ? `<div><strong>计费:</strong> ${cfg.pricing.input}/${cfg.pricing.output} ${cfg.pricing.currency}</div>` : ''}
                            </div>
                        `;
                        modeInfo = `
                            <span class="badge badge-success">Direct API</span>
                            ${cfg.passthrough ? '<span class="badge badge-info">透传</span>' : ''}
                            ${cfg.api_type === 'gemini_native' ? '<span class="badge badge-info">Gemini原生</span>' : ''}
                            ${cfg.image_compression?.enabled ? '<span class="badge" style="background: rgba(147, 51, 234, 0.2); color: #a855f7; border-color: rgba(147, 51, 234, 0.3);">🖼️压缩</span>' : ''}
                        `;
                } else {
                    configInfo = `
                        <div style="font-size: 0.875rem;">
                            <div><strong>Session:</strong> <code style="color: var(--accent);">...${cfg.session_id?.slice(-8) || 'N/A'}</code></div>
                            ${cfg.type ? `<div><strong>类型:</strong> ${cfg.type}</div>` : ''}
                        </div>
                    `;
                    modeInfo = `
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
                                ${isDirectAPI ? 'API' : 'LMArena'}
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

function showAddModelModal() {
    currentEditingModel = null;
    document.getElementById('modal-title').textContent = '添加模型';
    document.getElementById('model-name').value = '';
    document.getElementById('model-name').disabled = false;
    
    // 重置所有字段
    document.getElementById('session-id').value = '';
    document.getElementById('mode').value = 'direct_chat';
    document.getElementById('battle-target').value = 'A';
    document.getElementById('battle-target-group').style.display = 'none';
    document.getElementById('model-type').value = 'text';
    document.getElementById('api-type').value = 'direct_api';
    document.getElementById('api-base-url').value = '';
    document.getElementById('api-key').value = '';
    document.getElementById('model-id').value = '';
    document.getElementById('display-name').value = '';
    document.getElementById('passthrough').checked = true;
    document.getElementById('enable-prefix').checked = false;
    document.getElementById('enable-thinking').checked = true;
    document.getElementById('thinking-budget').value = '20000';
    document.getElementById('thinking-separator').value = '';
    document.getElementById('pricing-input').value = '';
    document.getElementById('pricing-output').value = '';
    document.getElementById('pricing-unit').value = '1000000';
    document.getElementById('pricing-currency').value = 'USD';
    document.getElementById('custom-params').value = '';
    document.getElementById('max-temperature').value = '';
    document.getElementById('lmarena-max-temperature').value = '';
    
    // 重置图片压缩配置
    resetImageCompressionFields();
    
    document.getElementById('config-type').value = 'lmarena';
    toggleConfigType();
    
    document.getElementById('model-modal').classList.add('active');
}

// 重置图片压缩字段
function resetImageCompressionFields() {
    document.getElementById('img-compression-enabled').checked = false;
    document.getElementById('img-target-format').value = '';
    document.getElementById('img-quality').value = '';
    document.getElementById('img-target-size-kb').value = '';
    document.getElementById('img-max-width').value = '';
    document.getElementById('img-max-height').value = '';
    document.getElementById('img-convert-png-to-jpg').checked = false;
    document.getElementById('img-compression-options').style.display = 'none';
}

// 切换图片压缩选项显示
function toggleImageCompressionOptions() {
    const enabled = document.getElementById('img-compression-enabled').checked;
    document.getElementById('img-compression-options').style.display = enabled ? 'block' : 'none';
}

// 初始化图片压缩checkbox事件
document.addEventListener('DOMContentLoaded', function() {
    const imgCompressionCheckbox = document.getElementById('img-compression-enabled');
    if (imgCompressionCheckbox) {
        imgCompressionCheckbox.addEventListener('change', toggleImageCompressionOptions);
    }
});

// 加载图片压缩配置到表单
function loadImageCompressionConfig(imgConfig) {
    if (imgConfig && imgConfig.enabled) {
        document.getElementById('img-compression-enabled').checked = true;
        document.getElementById('img-compression-options').style.display = 'block';
        
        document.getElementById('img-target-format').value = imgConfig.target_format || '';
        document.getElementById('img-quality').value = imgConfig.quality || imgConfig.jpeg_quality || '';
        document.getElementById('img-target-size-kb').value = imgConfig.target_size_kb || '';
        document.getElementById('img-max-width').value = imgConfig.max_width || '';
        document.getElementById('img-max-height').value = imgConfig.max_height || '';
        document.getElementById('img-convert-png-to-jpg').checked = imgConfig.convert_png_to_jpg || false;
    } else {
        resetImageCompressionFields();
    }
}

// 获取图片压缩配置
function getImageCompressionConfig() {
    const enabled = document.getElementById('img-compression-enabled').checked;
    if (!enabled) {
        return null;
    }
    
    const config = {
        enabled: true
    };
    
    const targetFormat = document.getElementById('img-target-format').value;
    if (targetFormat) config.target_format = targetFormat;
    
    const quality = document.getElementById('img-quality').value;
    if (quality) config.quality = parseInt(quality);
    
    const targetSizeKb = document.getElementById('img-target-size-kb').value;
    if (targetSizeKb) config.target_size_kb = parseInt(targetSizeKb);
    
    const maxWidth = document.getElementById('img-max-width').value;
    if (maxWidth) config.max_width = parseInt(maxWidth);
    
    const maxHeight = document.getElementById('img-max-height').value;
    if (maxHeight) config.max_height = parseInt(maxHeight);
    
    const convertPngToJpg = document.getElementById('img-convert-png-to-jpg').checked;
    if (convertPngToJpg) config.convert_png_to_jpg = true;
    
    return config;
}

// 复制模型配置
function copyModel(name, config) {
    currentEditingModel = null;  // 设为null表示新建模式
    document.getElementById('modal-title').textContent = '复制模型';
    document.getElementById('model-name').value = name + '_copy';  // 默认添加_copy后缀
    document.getElementById('model-name').disabled = false;  // 允许修改名称
    
    // 复用editModel的配置填充逻辑
    fillModelForm(config);
    
    document.getElementById('model-modal').classList.add('active');
    
    // 聚焦到模型名称输入框，方便用户修改
    setTimeout(() => {
        const nameInput = document.getElementById('model-name');
        nameInput.focus();
        nameInput.select();
    }, 100);
}

function editModel(name, config) {
    currentEditingModel = name;
    document.getElementById('modal-title').textContent = '编辑模型';
    document.getElementById('model-name').value = name;
    document.getElementById('model-name').disabled = true;
    
    fillModelForm(config);
    
    document.getElementById('model-modal').classList.add('active');
}

// 填充模型表单（editModel和copyModel共用）
function fillModelForm(config) {
    
    if (config.api_type === 'direct_api' || config.api_type === 'gemini_native') {
        document.getElementById('config-type').value = 'direct_api';
        document.getElementById('api-type').value = config.api_type || 'direct_api';
        document.getElementById('api-base-url').value = config.api_base_url || '';
        document.getElementById('api-key').value = config.api_key || '';
        document.getElementById('model-id').value = config.model_id || '';
        document.getElementById('display-name').value = config.display_name || '';
        document.getElementById('passthrough').checked = config.passthrough !== false;
        document.getElementById('enable-prefix').checked = config.enable_prefix || false;
        document.getElementById('enable-thinking').checked = config.enable_thinking !== false;
        document.getElementById('thinking-budget').value = config.thinking_budget || 20000;
        document.getElementById('thinking-separator').value = config.thinking_separator || '';
        
        // 加载自定义参数
        if (config.custom_params) {
            document.getElementById('custom-params').value = JSON.stringify(config.custom_params, null, 2);
        } else {
            document.getElementById('custom-params').value = '';
        }
        
        if (config.pricing) {
            document.getElementById('pricing-input').value = config.pricing.input || '';
            document.getElementById('pricing-output').value = config.pricing.output || '';
            document.getElementById('pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('pricing-currency').value = config.pricing.currency || 'USD';
        } else {
            // 重置计费配置
            document.getElementById('pricing-input').value = '';
            document.getElementById('pricing-output').value = '';
            document.getElementById('pricing-unit').value = '1000000';
            document.getElementById('pricing-currency').value = 'USD';
        }
        
        // 加载最高温度限制
        document.getElementById('max-temperature').value = config.max_temperature || '';
        
        // 加载图片压缩配置
        loadImageCompressionConfig(config.image_compression);
    } else {
        document.getElementById('config-type').value = 'lmarena';
        document.getElementById('session-id').value = config.session_id || '';
        document.getElementById('mode').value = config.mode || 'direct_chat';
        document.getElementById('battle-target').value = config.battle_target || 'A';
        document.getElementById('model-type').value = config.type || 'text';
        document.getElementById('lmarena-display-name').value = config.display_name || '';
        
        if (config.pricing) {
            document.getElementById('lmarena-pricing-input').value = config.pricing.input || '';
            document.getElementById('lmarena-pricing-output').value = config.pricing.output || '';
            document.getElementById('lmarena-pricing-unit').value = config.pricing.unit || 1000000;
            document.getElementById('lmarena-pricing-currency').value = config.pricing.currency || 'USD';
        } else {
            // 重置计费配置
            document.getElementById('lmarena-pricing-input').value = '';
            document.getElementById('lmarena-pricing-output').value = '';
            document.getElementById('lmarena-pricing-unit').value = '1000000';
            document.getElementById('lmarena-pricing-currency').value = 'USD';
        }
        
        // 加载最高温度限制
        document.getElementById('lmarena-max-temperature').value = config.max_temperature || '';
        
        toggleBattleTarget();
    }
    
    toggleConfigType();
}

function closeModelModal() {
    document.getElementById('model-modal').classList.remove('active');
}

async function saveModel() {
    const modelName = document.getElementById('model-name').value.trim();
    const configType = document.getElementById('config-type').value;
    
    if (!modelName) {
        alert('请输入模型名称');
        return;
    }
    
    let config = {};
    
    if (configType === 'direct_api') {
        const apiBaseUrl = document.getElementById('api-base-url').value.trim();
        const apiKey = document.getElementById('api-key').value.trim();
        const apiType = document.getElementById('api-type').value;
        
        // 🔧 修复：API Key变为可选（支持本地反代等无需认证的场景）
        // 但 OpenAI 兼容格式仍然需要 api_base_url
        if (apiType === 'direct_api' && !apiBaseUrl) {
            alert('OpenAI兼容格式需要填写 API Base URL');
            return;
        }
        
        // Gemini原生格式既不需要api_base_url也不需要api_key（可以使用默认地址）
        // 但如果两者都没填，给个警告
        if (apiType === 'gemini_native' && !apiBaseUrl && !apiKey) {
            if (!confirm('未填写API Base URL和API Key，将使用Google官方地址。确定继续？')) {
                return;
            }
        }
        
        const thinkingSeparator = document.getElementById('thinking-separator').value.trim();
        
        config = {
            api_type: apiType,
            model_id: document.getElementById('model-id').value.trim() || modelName,
            display_name: document.getElementById('display-name').value.trim() || modelName,
            passthrough: document.getElementById('passthrough').checked,
            enable_prefix: document.getElementById('enable-prefix').checked,
            enable_thinking: document.getElementById('enable-thinking').checked,
            thinking_budget: parseInt(document.getElementById('thinking-budget').value) || 20000
        };
        
        // 🔧 只在有值时添加api_base_url和api_key字段
        if (apiBaseUrl) {
            config.api_base_url = apiBaseUrl;
        }
        if (apiKey) {
            config.api_key = apiKey;
        }
        
        // 只在有值时添加thinking_separator字段
        if (thinkingSeparator) {
            config.thinking_separator = thinkingSeparator;
        }
        
        // 处理自定义参数
        const customParamsStr = document.getElementById('custom-params').value.trim();
        if (customParamsStr) {
            try {
                const customParams = JSON.parse(customParamsStr);
                config.custom_params = customParams;
            } catch (e) {
                alert('自定义参数格式错误，请输入有效的JSON格式\n错误: ' + e.message);
                return;
            }
        }
        
        const pricingInput = document.getElementById('pricing-input').value;
        const pricingOutput = document.getElementById('pricing-output').value;
        
        if (pricingInput || pricingOutput) {
            config.pricing = {
                input: parseFloat(pricingInput) || 0,
                output: parseFloat(pricingOutput) || 0,
                unit: parseInt(document.getElementById('pricing-unit').value) || 1000000,
                currency: document.getElementById('pricing-currency').value
            };
        }
        
        // 保存最高温度限制
        const maxTemperature = document.getElementById('max-temperature').value.trim();
        if (maxTemperature) {
            config.max_temperature = parseFloat(maxTemperature);
        }
        
        // 保存图片压缩配置
        const imageCompressionConfig = getImageCompressionConfig();
        if (imageCompressionConfig) {
            config.image_compression = imageCompressionConfig;
        }
    } else {
        const sessionId = document.getElementById('session-id').value.trim();
        const mode = document.getElementById('mode').value;
        
        if (!sessionId) {
            alert('请填写Session ID');
            return;
        }
        
        config = {
            session_id: sessionId,
            mode: mode,
            type: document.getElementById('model-type').value,
            display_name: document.getElementById('lmarena-display-name').value.trim() || modelName
        };
        
        if (mode === 'battle') {
            config.battle_target = document.getElementById('battle-target').value;
        }
        
        const lmarenaPricingInput = document.getElementById('lmarena-pricing-input').value;
        const lmarenaPricingOutput = document.getElementById('lmarena-pricing-output').value;
        
        if (lmarenaPricingInput || lmarenaPricingOutput) {
            config.pricing = {
                input: parseFloat(lmarenaPricingInput) || 0,
                output: parseFloat(lmarenaPricingOutput) || 0,
                unit: parseInt(document.getElementById('lmarena-pricing-unit').value) || 1000000,
                currency: document.getElementById('lmarena-pricing-currency').value
            };
        }
        
        // 保存最高温度限制
        const lmarenaMaxTemperature = document.getElementById('lmarena-max-temperature').value.trim();
        if (lmarenaMaxTemperature) {
            config.max_temperature = parseFloat(lmarenaMaxTemperature);
        }
    }
    
    try {
        const response = await fetch('/api/admin/models', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ model_name: modelName, config: config })
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
        
        closeModelModal();
        loadModels();
        showMessage('success', `模型 ${modelName} 保存成功`);
        
    } catch (error) {
        console.error('❌ 保存模型失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '保存失败: ' + error.message);
    }
}

async function deleteModel(name) {
    if (!confirm(`确定要删除模型 "${name}" 吗？`)) return;
    
    try {
        const response = await fetch(`/api/admin/models/${encodeURIComponent(name)}`, {
            method: 'DELETE'
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

// ==================== ID 捕获 ====================
function selectCaptureMode(mode, element) {
    selectedCaptureMode = mode;
    document.querySelectorAll('.radio-card').forEach(card => card.classList.remove('selected'));
    element.classList.add('selected');
    document.getElementById('battle-target-selection').style.display = mode === 'battle' ? 'block' : 'none';
}

function selectBattleTarget(target, element) {
    selectedBattleTarget = target;
    document.querySelectorAll('input[name="battle_target"]').forEach(radio => {
        radio.parentElement.classList.remove('selected');
    });
    element.classList.add('selected');
}

async function startIdCapture() {
    const statusEl = document.getElementById('capture-status');
    statusEl.innerHTML = '<div style="color: var(--accent);">⏳ 正在启动捕获...</div>';
    
    try {
        const response = await fetch('/internal/start_id_capture', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                mode: selectedCaptureMode,
                battle_target: selectedBattleTarget
            })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            statusEl.innerHTML = `
                <div class="alert alert-info">
                    <span>⏳</span>
                    <div>
                        <strong>捕获已启动，等待浏览器响应...</strong><br>
                        模式: ${result.mode === 'battle' ? 'Battle' : 'Direct Chat'}<br>
                        ${result.mode === 'battle' ? `目标: ${result.battle_target}<br>` : ''}
                        <small style="opacity: 0.8; color: var(--accent);">
                            ⚠️ 请在LMArena页面找到已有对话，点击<strong>Retry按钮</strong>（刷新图标）
                        </small>
                    </div>
                </div>
            `;
            
            startCapturePolling(statusEl);
        } else {
            throw new Error(result.detail || '启动失败');
        }
        
    } catch (error) {
        console.error('启动捕获失败:', error);
        statusEl.innerHTML = `
            <div class="alert alert-danger">
                <span>❌</span>
                <div>启动失败: ${error.message}</div>
            </div>
        `;
    }
}

function startCapturePolling(statusEl) {
    if (capturePollingInterval) clearInterval(capturePollingInterval);
    
    let pollCount = 0;
    const maxPolls = 60;
    
    capturePollingInterval = setInterval(async () => {
        pollCount++;
        
        try {
            const response = await fetch('/api/admin/capture_status');
            const status = await response.json();
            
            if (status.captured && status.session_id && status.message_id) {
                clearInterval(capturePollingInterval);
                
                statusEl.innerHTML = `
                    <div class="alert alert-success">
                        <span>🎉</span>
                        <div>
                            <strong>ID捕获成功！</strong><br>
                            正在打开配置窗口...
                        </div>
                    </div>
                `;
                
                setTimeout(() => showCaptureConfigModal(status), 500);
            } else if (pollCount >= maxPolls) {
                clearInterval(capturePollingInterval);
                statusEl.innerHTML = `
                    <div class="alert alert-danger">
                        <span>⏱️</span>
                        <div>
                            <strong>捕获超时</strong><br>
                            未在60秒内收到响应，请重试
                        </div>
                    </div>
                `;
            }
        } catch (error) {
            console.error('检查捕获状态失败:', error);
        }
    }, 1000);
}

function showCaptureConfigModal(captureStatus) {
    document.getElementById('captured-ids-display').innerHTML = `
        Session ID: <code style="color: var(--accent);">...${captureStatus.session_id.slice(-8)}</code><br>
        Message ID: <code style="color: var(--accent);">...${captureStatus.message_id.slice(-8)}</code>
    `;
    
    const modeText = captureStatus.mode === 'battle' ? 'Battle' : 'Direct Chat';
    document.getElementById('capture-mode-display').textContent = modeText;
    
    const targetDisplay = document.getElementById('capture-target-display');
    if (captureStatus.mode === 'battle') {
        targetDisplay.innerHTML = `目标: ${captureStatus.battle_target} (${captureStatus.battle_target === 'A' ? '左侧' : '右侧'}模型)`;
        targetDisplay.style.display = 'block';
    } else {
        targetDisplay.style.display = 'none';
    }
    
    document.getElementById('capture-model-name').value = '';
    document.getElementById('capture-model-type').value = 'text';
    
    document.getElementById('capture-config-modal').classList.add('active');
}

function closeCaptureConfigModal() {
    document.getElementById('capture-config-modal').classList.remove('active');
}

async function saveCapturedModel() {
    const modelName = document.getElementById('capture-model-name').value.trim();
    const modelType = document.getElementById('capture-model-type').value;
    
    if (!modelName) {
        alert('请输入模型名称');
        return;
    }
    
    try {
        const response = await fetch('/api/admin/save_captured_model', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                model_name: modelName,
                model_type: modelType
            })
        });
        
        const result = await response.json();
        
        if (response.ok) {
            closeCaptureConfigModal();
            showMessage('success', `模型 ${modelName} 配置已保存`);
            loadModels();
            document.getElementById('capture-status').innerHTML = '';
        } else {
            throw new Error(result.detail || '保存失败');
        }
    } catch (error) {
        console.error('保存模型配置失败:', error);
        alert('保存失败: ' + error.message);
    }
}

// ==================== Direct API 模型列表获取 ====================

// 保存完整的模型列表用于搜索过滤
let allFetchedModels = [];

async function fetchModelsFromAPI(event) {
    const apiBaseUrl = document.getElementById('api-base-url').value.trim();
    const apiKey = document.getElementById('api-key').value.trim();
    
    if (!apiBaseUrl) {
        alert('请先填写API Base URL');
        return;
    }
    
    // 🔧 API Key变为可选（支持本地反代等无需认证的场景）
    const hasApiKey = apiKey.length > 0;
    if (!hasApiKey) {
        console.log('⚠️ 未提供API Key，将尝试无认证请求（适用于本地反代）');
    }
    
    // 🔧 关键修复：在函数开始就保存按钮引用
    const button = event.target;
    const originalText = button.innerHTML;
    
    try {
        // 显示加载状态
        button.innerHTML = '⏳ 加载中...';
        button.disabled = true;
        
        // 智能构建/models端点URL
        // 移除末尾的斜杠
        let baseUrl = apiBaseUrl.replace(/\/+$/, '');
        
        // 🔧 检测是否是Google Gemini API
        const isGeminiAPI = baseUrl.includes('generativelanguage.googleapis.com');
        
        // 尝试多种可能的URL模式
        const urlsToTry = [];
        
        if (isGeminiAPI) {
            // Google Gemini API特殊处理
            // 使用查询参数传递API Key
            urlsToTry.push(`${baseUrl}/models?key=${encodeURIComponent(apiKey)}`);
            if (!baseUrl.includes('/v1')) {
                urlsToTry.push(`https://generativelanguage.googleapis.com/v1beta/models?key=${encodeURIComponent(apiKey)}`);
            }
        } else {
            // 标准OpenAI格式
            urlsToTry.push(`${baseUrl}/models`);
            
            // 如果baseUrl不包含/v1，也尝试添加/v1/models
            if (!baseUrl.includes('/v1')) {
                urlsToTry.push(`${baseUrl}/v1/models`);
            }
        }
        
        let lastError = null;
        let successfulUrl = null;
        let data = null;
        
        // 依次尝试每个URL
        for (const modelsUrl of urlsToTry) {
            try {
                console.log(`尝试获取模型列表: ${modelsUrl.replace(/key=[^&]+/, 'key=***')}`);
                
                const headers = {
                    'Content-Type': 'application/json'
                };
                
                // 🔧 Gemini使用query参数，其他API使用Bearer Token
                if (!isGeminiAPI) {
                    headers['Authorization'] = `Bearer ${apiKey}`;
                }
                
                const response = await fetch(modelsUrl, {
                    method: 'GET',
                    headers: headers
                });
                
                if (response.ok) {
                    data = await response.json();
                    successfulUrl = modelsUrl;
                    console.log(`✅ 成功从 ${modelsUrl.replace(/key=[^&]+/, 'key=***')} 获取模型列表`);
                    break; // 成功则退出循环
                } else {
                    lastError = `HTTP ${response.status}: ${response.statusText}`;
                    console.warn(`❌ ${modelsUrl.replace(/key=[^&]+/, 'key=***')} 返回错误: ${lastError}`);
                }
            } catch (err) {
                lastError = err.message;
                console.warn(`❌ ${modelsUrl.replace(/key=[^&]+/, 'key=***')} 请求失败: ${lastError}`);
            }
        }
        
        // 如果所有URL都失败
        if (!successfulUrl || !data) {
            throw new Error(
                `无法从API获取模型列表。\n` +
                `已尝试的URL：\n${urlsToTry.join('\n')}\n\n` +
                `最后错误: ${lastError}\n\n` +
                `请检查：\n` +
                `1. API Base URL是否正确（例如：https://api.deepseek.com）\n` +
                `2. API Key是否有效\n` +
                `3. 该API是否支持 /models 端点`
            );
        }
        
        // 解析模型列表（兼容OpenAI格式）
        let models = [];
        if (data.data && Array.isArray(data.data)) {
            models = data.data.map(m => m.id || m.name || m);
        } else if (Array.isArray(data)) {
            models = data;
        } else if (data.models && Array.isArray(data.models)) {
            // Gemini格式：从 models 数组中提取名称
            models = data.models.map(m => {
                // Gemini返回格式：{ name: "models/gemini-2.0-flash", displayName: "..." }
                if (typeof m === 'object' && m.name) {
                    // 移除 "models/" 前缀
                    return m.name.replace(/^models\//, '');
                }
                return m.id || m.name || m;
            });
        } else {
            console.error('无法解析的响应格式:', data);
            throw new Error(`无法解析模型列表格式。响应结构: ${JSON.stringify(Object.keys(data))}`);
        }
        
        if (models.length === 0) {
            alert('未获取到任何模型，请检查API是否返回了模型列表');
            button.innerHTML = originalText;
            button.disabled = false;
            return;
        }
        
        // 保存完整的模型列表用于搜索
        allFetchedModels = models;
        
        // 填充下拉框
        const selectContainer = document.getElementById('model-select-container');
        const select = document.getElementById('model-select');
        const searchInput = document.getElementById('model-search');
        const countSpan = document.getElementById('model-search-count');
        
        // 清空搜索框
        if (searchInput) {
            searchInput.value = '';
        }
        
        // 更新下拉框
        populateModelSelect(models);
        
        // 更新计数
        if (countSpan) {
            countSpan.textContent = `${models.length} 个模型`;
        }
        
        // 显示下拉框
        selectContainer.style.display = 'block';
        
        // 恢复按钮状态
        button.innerHTML = originalText;
        button.disabled = false;
        
        // 显示成功消息
        showMessage('success', `✅ 成功从 ${successfulUrl} 获取 ${models.length} 个模型`);
        
    } catch (error) {
        console.error('获取模型列表失败:', error);
        alert(error.message);
        
        // 恢复按钮状态
        const button = event.target;
        button.innerHTML = '📋 从API获取';
        button.disabled = false;
    }
}

// 填充模型下拉框
function populateModelSelect(models) {
    const select = document.getElementById('model-select');
    select.innerHTML = '';
    
    models.forEach(modelId => {
        const option = document.createElement('option');
        option.value = modelId;
        option.textContent = modelId;
        select.appendChild(option);
    });
}

// 过滤模型列表
function filterModelList() {
    const searchInput = document.getElementById('model-search');
    const countSpan = document.getElementById('model-search-count');
    const searchTerm = searchInput.value.toLowerCase().trim();
    
    if (!searchTerm) {
        // 如果搜索框为空，显示所有模型
        populateModelSelect(allFetchedModels);
        if (countSpan) {
            countSpan.textContent = `${allFetchedModels.length} 个模型`;
        }
        return;
    }
    
    // 过滤模型
    const filteredModels = allFetchedModels.filter(modelId =>
        modelId.toLowerCase().includes(searchTerm)
    );
    
    // 更新下拉框
    populateModelSelect(filteredModels);
    
    // 更新计数
    if (countSpan) {
        countSpan.textContent = `${filteredModels.length} / ${allFetchedModels.length}`;
    }
}

function selectModelFromList() {
    const select = document.getElementById('model-select');
    const selectedModel = select.value;
    
    if (selectedModel) {
        document.getElementById('model-id').value = selectedModel;
    }
}