// admin-models-capture.js - 自动抓取模型、键测试
'use strict';


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
                        ${result.mode === 'battle' ? `目标: ${escapeHtml(result.battle_target)}<br>` : ''}
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
                <div>启动失败: ${escapeHtml(error.message)}</div>
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
            
            if (status.captured && status.session_id) {
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
        Session ID: <code style="color: var(--accent);">...${escapeHtml(captureStatus.session_id.slice(-8))}</code><br>
        <span style="color: var(--text-dim);">仅用于已弃用但保留兼容的 LMArena 模式</span>
    `;
    
    const modeText = captureStatus.mode === 'battle' ? 'Battle' : 'Direct Chat';
    document.getElementById('capture-mode-display').textContent = modeText;
    
    const targetDisplay = document.getElementById('capture-target-display');
    if (captureStatus.mode === 'battle') {
        targetDisplay.innerHTML = `目标: ${escapeHtml(captureStatus.battle_target)} (${captureStatus.battle_target === 'A' ? '左侧' : '右侧'}模型)`;
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
    // 🔧 多 API Key 轮询：使用第一个非空 key
    const apiKeys = getApiKeys();
    const apiKey = apiKeys.length > 0 ? apiKeys[0] : '';
    
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

// ==================== 测试当前模型的所有 Key ====================

async function testCurrentModelKeys() {
    const btn = document.getElementById('test-keys-btn');
    const resultsDiv = document.getElementById('key-test-results');
    
    // 收集当前编辑框中的配置
    const apiKeys = getApiKeys();
    if (apiKeys.length === 0) {
        alert('请先填写至少一个 API Key');
        return;
    }
    
    const apiBaseUrl = document.getElementById('api-base-url').value.trim();
    const apiType = document.getElementById('api-type').value;
    const modelId = document.getElementById('model-id').value.trim() || document.getElementById('model-name').value.trim();
    const defaultEndpoints = {
        responses_native: '/responses',
        anthropic_native: '/messages',
    };
    const defaultEndpoint = defaultEndpoints[apiType] || '/chat/completions';
    const endpointPath = document.getElementById('endpoint-path').value.trim() || defaultEndpoint;
    
    if (apiType !== 'gemini_native' && !apiBaseUrl) {
        alert('请先填写 API Base URL');
        return;
    }
    
    // 显示加载状态
    btn.disabled = true;
    btn.innerHTML = '⏳ 测试中...';
    resultsDiv.style.display = 'block';
    resultsDiv.innerHTML = `<div style="text-align: center; padding: 10px; color: var(--text-dim);">⏳ 正在并行测试 ${apiKeys.length} 个 Key...</div>`;
    
    try {
        const response = await fetch('/api/admin/test_model_keys', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                api_keys: apiKeys,
                api_base_url: apiBaseUrl,
                model_id: modelId,
                api_type: apiType,
                endpoint_path: endpointPath,
            })
        });
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        
        // 渲染结果
        let html = `<div style="font-size: 0.85rem; margin-bottom: 8px; color: var(--text-dim);">${escapeHtml(data.message)}</div>`;
        html += '<div style="display: flex; flex-direction: column; gap: 6px;">';
        
        for (const r of data.results) {
            let icon, color, bg;
            if (r.status === 'ok') {
                icon = '✅'; color = '#10b981'; bg = 'rgba(16,185,129,0.1)';
            } else if (r.status === 'timeout') {
                icon = '⏱️'; color = '#f59e0b'; bg = 'rgba(245,158,11,0.1)';
            } else {
                icon = '❌'; color = '#ef4444'; bg = 'rgba(239,68,68,0.1)';
            }
            
            const timeStr = r.response_time_ms ? `${r.response_time_ms}ms` : '';
            const errStr = r.error ? ` — ${escapeHtml(r.error)}` : '';
            
            html += `
                <div style="display: flex; align-items: center; gap: 8px; padding: 6px 10px; background: ${bg}; border-radius: 4px; font-size: 0.8rem;">
                    <span>${icon}</span>
                    <code style="color: ${color}; font-family: monospace;">${escapeHtml(r.key_preview)}</code>
                    <span style="color: var(--text-dim);">${timeStr}</span>
                    <span style="color: ${color}; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${errStr}</span>
                </div>
            `;
        }
        html += '</div>';
        resultsDiv.innerHTML = html;
        
    } catch (error) {
        resultsDiv.innerHTML = `<div style="color: #ef4444; font-size: 0.85rem;">❌ 测试失败: ${escapeHtml(error.message)}</div>`;
    } finally {
        btn.disabled = false;
        btn.innerHTML = '🔑 测试所有 Key';
    }
}
