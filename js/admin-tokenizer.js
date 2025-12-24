// admin-tokenizer.js - Tokenizer配置功能

async function refreshTokenizerInfo() {
    try {
        const response = await fetch('/api/admin/tokenizer_info');
        
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
        
        const info = await response.json();
        
        document.getElementById('tokenizer-status').innerHTML = `
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
                <div>
                    <strong>Tiktoken:</strong>
                    <span class="badge ${info.tiktoken_available ? 'badge-success' : 'badge-danger'}">
                        ${info.tiktoken_available ? '✓ 已安装' : '✗ 未安装'}
                    </span>
                </div>
                <div>
                    <strong>计数方法:</strong>
                    <span class="badge badge-info">${info.method}</span>
                </div>
                <div>
                    <strong>缓存模型数:</strong> ${info.cached_models.length}
                </div>
            </div>
        `;
    } catch (error) {
        console.error('❌ 刷新Tokenizer信息失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新Tokenizer信息失败: ' + error.message);
    }
}

async function loadTokenizerMappings() {
    try {
        const [modelsResponse, tokenizerResponse] = await Promise.all([
            fetch('/api/admin/models'),
            fetch('/api/admin/tokenizer_mappings')
        ]);
        
        if (!modelsResponse.ok) {
            const errorText = await modelsResponse.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${modelsResponse.status}): ${errorDetail}`);
        }
        
        if (!tokenizerResponse.ok) {
            const errorText = await tokenizerResponse.text();
            let errorDetail;
            try {
                const errorJson = JSON.parse(errorText);
                errorDetail = errorJson.detail || errorJson.message || errorText;
            } catch {
                errorDetail = errorText;
            }
            throw new Error(`API错误 (${tokenizerResponse.status}): ${errorDetail}`);
        }
        
        const modelsData = await modelsResponse.json();
        const tokenizerConfig = await tokenizerResponse.json();
        
        const container = document.getElementById('tokenizer-mappings-list');
        const modelEndpointMap = modelsData.model_endpoint_map;
        
        if (!modelEndpointMap || Object.keys(modelEndpointMap).length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🤖</div><p>还没有配置任何模型<br/>请先在"模型端点"页面添加模型</p></div>';
            return;
        }
        
        container.innerHTML = `
            <table class="table">
                <thead>
                    <tr>
                        <th>模型名称</th>
                        <th>当前分词器</th>
                        <th>选择分词器类型</th>
                        <th>操作</th>
                    </tr>
                </thead>
                <tbody>
                    ${Object.keys(modelEndpointMap).map(modelName => {
                        const currentTokenizer = tokenizerConfig[modelName] || getDefaultTokenizer(modelName);
                        const hasCustomConfig = tokenizerConfig.hasOwnProperty(modelName);
                        return `
                            <tr>
                                <td><strong>${modelName}</strong></td>
                                <td>
                                    <span class="badge ${
                                        currentTokenizer === 'anthropic' ? 'badge-info' :
                                        currentTokenizer === 'google' ? 'badge-success' :
                                        currentTokenizer === 'tiktoken' ? 'badge-info' :
                                        currentTokenizer === 'deepseek' ? 'badge-success' :
                                        'badge-danger'
                                    }">${currentTokenizer}</span>
                                    ${!hasCustomConfig ? '<span class="badge badge-info" style="margin-left: 5px;">默认</span>' : ''}
                                </td>
                                <td>
                                    <select class="form-select" data-model="${modelName}" style="width: auto; display: inline-block;">
                                        <option value="tiktoken" ${currentTokenizer === 'tiktoken' ? 'selected' : ''}>tiktoken</option>
                                        <option value="anthropic" ${currentTokenizer === 'anthropic' ? 'selected' : ''}>anthropic</option>
                                        <option value="google" ${currentTokenizer === 'google' ? 'selected' : ''}>google</option>
                                        <option value="deepseek" ${currentTokenizer === 'deepseek' ? 'selected' : ''}>deepseek</option>
                                        <option value="estimate" ${currentTokenizer === 'estimate' ? 'selected' : ''}>estimate</option>
                                    </select>
                                </td>
                                <td>
                                    ${hasCustomConfig ?
                                        `<button class="btn btn-danger btn-sm" onclick="deleteTokenizerConfig('${modelName}')">🗑️ 删除</button>` :
                                        `<span style="color: var(--text-dim); font-size: 0.875rem;">使用默认</span>`
                                    }
                                </td>
                            </tr>
                        `;
                    }).join('')}
                </tbody>
            </table>
        `;
    } catch (error) {
        console.error('❌ 加载Tokenizer映射失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '加载失败: ' + error.message);
    }
}

function getDefaultTokenizer(modelName) {
    const lower = modelName.toLowerCase();
    if (lower.includes('claude')) return 'anthropic';
    if (lower.includes('gemini')) return 'google';
    if (lower.includes('gpt')) return 'tiktoken';
    if (lower.includes('deepseek')) return 'deepseek';
    return 'tiktoken';
}

async function saveAllTokenizerSettings() {
    try {
        const selects = document.querySelectorAll('[data-model]');
        const newConfig = {};
        
        selects.forEach(select => {
            const modelName = select.getAttribute('data-model');
            const tokenizerType = select.value;
            newConfig[modelName] = tokenizerType;
        });
        
        const response = await fetch('/api/admin/tokenizer_mappings', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tokenizer_config: newConfig })
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
        
        showMessage('success', '✅ 所有分词器设置已保存');
        loadTokenizerMappings();
        refreshTokenizerInfo();
        
    } catch (error) {
        console.error('❌ 保存分词器设置失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '保存失败: ' + error.message);
    }
}