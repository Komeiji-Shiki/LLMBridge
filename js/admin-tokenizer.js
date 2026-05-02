// admin-tokenizer.js - Tokenizer配置功能

async function refreshTokenizerInfo() {
    try {
        // 获取详细的分词器状态
        const response = await fetch('/api/admin/tokenizers_status');
        
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
        
        const status = await response.json();
        
        // 构建分词器状态卡片
        let statusHTML = `
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px; padding: 10px 0;">
        `;
        
        // 按顺序显示各分词器
        const tokenizerOrder = ['tiktoken', 'anthropic', 'transformers', 'google_generativeai', 'gemma_local', 'deepseek_local'];
        
        for (const key of tokenizerOrder) {
            const tokenizer = status[key];
            if (!tokenizer) continue;
            
            const isAvailable = tokenizer.available;
            const badgeClass = isAvailable ? 'badge-success' : 'badge-danger';
            const statusIcon = isAvailable ? '✓' : '✗';
            const statusText = isAvailable ? '已安装' : '未安装';
            
            statusHTML += `
                <div style="padding: 15px; background: var(--surface-2); border-radius: 6px; border: 1px solid var(--line-weak);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                        <strong style="color: var(--text-main);">${tokenizer.name}</strong>
                        <span class="badge ${badgeClass}">${statusIcon} ${statusText}</span>
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-dim); margin-bottom: 8px;">
                        ${tokenizer.description || ''}
                    </div>
            `;
            
            if (isAvailable && tokenizer.version) {
                statusHTML += `
                    <div style="font-size: 0.75rem; color: var(--accent);">
                        版本: ${tokenizer.version}
                    </div>
                `;
            }
            
            if (isAvailable && tokenizer.path) {
                statusHTML += `
                    <div style="font-size: 0.75rem; color: var(--accent); word-break: break-all;">
                        路径: ${tokenizer.path}
                    </div>
                `;
            }
            
            if (!isAvailable && tokenizer.install_cmd) {
                statusHTML += `
                    <div style="margin-top: 8px;">
                        <code style="font-size: 0.75rem; background: var(--surface); padding: 4px 8px; border-radius: 4px; color: #f59e0b;">
                            ${tokenizer.install_cmd}
                        </code>
                    </div>
                `;
            }
            
            if (tokenizer.supported_models && tokenizer.supported_models.length > 0) {
                statusHTML += `
                    <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 8px;">
                        支持模型: ${tokenizer.supported_models.join(', ')}
                    </div>
                `;
            }
            
            statusHTML += `</div>`;
        }
        
        statusHTML += `</div>`;
        
        // 添加一键安装分词器区域
        statusHTML += `
            <div style="margin-top: 20px; padding: 20px; background: var(--surface-2); border: 1px solid var(--line-weak); border-radius: 6px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <div>
                        <strong style="color: var(--text-main); font-size: 1.1rem;">📦 安装分词器</strong>
                        <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">
                            点击按钮直接安装所需的分词器库（需要Python环境和pip）
                        </p>
                    </div>
                </div>
                
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px;">
                    ${buildInstallCard('tiktoken', 'Tiktoken', 'GPT-4, GPT-3.5', status.tiktoken?.available)}
                    ${buildInstallCard('anthropic', 'Anthropic', 'Claude-3系列', status.anthropic?.available)}
                    ${buildInstallCard('transformers', 'Transformers', 'Gemma, DeepSeek', status.transformers?.available)}
                    ${buildInstallCard('google-generativeai', 'Google AI', 'Gemini API', status.google_generativeai?.available)}
                </div>
                
                <div style="margin-top: 15px; padding: 12px; background: rgba(42, 168, 255, 0.1); border: 1px solid var(--accent-soft); border-radius: 6px;">
                    <div style="display: flex; justify-content: space-between; align-items: center;">
                        <div>
                            <strong style="color: var(--accent);">🚀 一键安装全部</strong>
                            <p style="font-size: 0.75rem; color: var(--text-dim); margin-top: 3px;">
                                安装所有未安装的分词器库
                            </p>
                        </div>
                        <button class="btn btn-primary" onclick="installAllTokenizers()" id="install-all-btn">
                            安装全部
                        </button>
                    </div>
                </div>
                
                <div id="install-progress" style="display: none; margin-top: 15px; padding: 12px; background: var(--surface); border-radius: 6px;">
                    <div style="display: flex; align-items: center; gap: 10px;">
                        <div class="loading-spinner" style="width: 20px; height: 20px; border: 2px solid var(--line-strong); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite;"></div>
                        <span id="install-progress-text" style="color: var(--text-dim);">正在安装...</span>
                    </div>
                </div>
                
                <div id="install-result" style="display: none; margin-top: 15px;"></div>
                
                <div style="margin-top: 15px; padding: 10px; background: rgba(245, 158, 11, 0.1); border-left: 3px solid #f59e0b; border-radius: 0 4px 4px 0;">
                    <strong style="color: #f59e0b; font-size: 0.85rem;">📁 本地Tokenizer文件</strong>
                    <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">
                        Gemma和DeepSeek的本地tokenizer需要手动下载文件放到对应目录：
                    </p>
                    <ul style="font-size: 0.75rem; color: var(--text-dim); margin-top: 5px; padding-left: 20px;">
                        <li>Gemma: <code style="background: var(--surface); padding: 2px 6px; border-radius: 3px;">tokenizers/gemma3-27b-it/</code></li>
                        <li>DeepSeek: <code style="background: var(--surface); padding: 2px 6px; border-radius: 3px;">deepseek_v3_tokenizer/</code></li>
                    </ul>
                </div>
            </div>
        `;
        
        // 添加Token计算器入口
        statusHTML += `
            <div style="margin-top: 20px; padding: 15px; background: rgba(42, 168, 255, 0.1); border: 1px solid var(--accent-soft); border-radius: 6px;">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <strong style="color: var(--accent);">🧮 Token 计算器</strong>
                        <p style="font-size: 0.875rem; color: var(--text-dim); margin-top: 5px;">
                            计算文本的token数量，对比不同分词器的结果
                        </p>
                    </div>
                    <button class="btn btn-primary" onclick="openTokenCalculator()">
                        打开计算器
                    </button>
                </div>
            </div>
        `;
        
        document.getElementById('tokenizer-status').innerHTML = statusHTML;
        
    } catch (error) {
        console.error('❌ 刷新Tokenizer信息失败:', error);
        console.error('错误详情:', error.message);
        showMessage('danger', '刷新Tokenizer信息失败: ' + error.message);
        
        // 回退到基本信息
        try {
            const basicResponse = await fetch('/api/admin/tokenizer_info');
            if (basicResponse.ok) {
                const info = await basicResponse.json();
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
            }
        } catch (fallbackError) {
            console.error('回退也失败:', fallbackError);
        }
    }
}

// 打开Token计算器
function openTokenCalculator() {
    window.open('/token_calculator', '_blank');
}

// 构建安装卡片HTML
function buildInstallCard(packageName, displayName, models, isInstalled) {
    const statusBadge = isInstalled
        ? '<span class="badge badge-success" style="font-size: 0.7rem;">✓ 已安装</span>'
        : '<span class="badge badge-danger" style="font-size: 0.7rem;">✗ 未安装</span>';
    
    const buttonHtml = isInstalled
        ? `<button class="btn btn-sm" disabled style="opacity: 0.5;">已安装</button>`
        : `<button class="btn btn-sm btn-primary" onclick="installTokenizer('${packageName}')" id="install-${packageName}-btn">
               安装
           </button>`;
    
    return `
        <div style="padding: 12px; background: var(--surface); border: 1px solid var(--line-weak); border-radius: 6px;">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px;">
                <strong style="color: var(--text-main);">${displayName}</strong>
                ${statusBadge}
            </div>
            <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 10px;">
                ${models}
            </div>
            ${buttonHtml}
        </div>
    `;
}

// 安装单个分词器
async function installTokenizer(packageName) {
    const btn = document.getElementById(`install-${packageName}-btn`);
    const progressDiv = document.getElementById('install-progress');
    const progressText = document.getElementById('install-progress-text');
    const resultDiv = document.getElementById('install-result');
    
    // 禁用按钮
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = '安装中...';
    }
    
    // 显示进度
    progressDiv.style.display = 'block';
    progressText.textContent = `正在安装 ${packageName}...`;
    resultDiv.style.display = 'none';
    
    try {
        const response = await fetch('/api/admin/install_tokenizer', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ package: packageName })
        });
        
        const result = await response.json();
        
        progressDiv.style.display = 'none';
        
        if (result.success) {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; color: #10b981;">
                    ✅ ${packageName} 安装成功！
                </div>
            `;
            showMessage('success', `${packageName} 安装成功！`);
            
            // 刷新分词器状态
            setTimeout(() => {
                refreshTokenizerInfo();
            }, 1000);
        } else {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                    ❌ 安装失败: ${result.error || '未知错误'}
                </div>
            `;
            showMessage('danger', `安装失败: ${result.error}`);
            
            // 恢复按钮
            if (btn) {
                btn.disabled = false;
                btn.innerHTML = '安装';
            }
        }
    } catch (error) {
        progressDiv.style.display = 'none';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `
            <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                ❌ 网络错误: ${error.message}
            </div>
        `;
        showMessage('danger', `网络错误: ${error.message}`);
        
        // 恢复按钮
        if (btn) {
            btn.disabled = false;
            btn.innerHTML = '安装';
        }
    }
}

// 安装所有未安装的分词器
async function installAllTokenizers() {
    const packages = ['tiktoken', 'anthropic', 'transformers', 'google-generativeai'];
    const progressDiv = document.getElementById('install-progress');
    const progressText = document.getElementById('install-progress-text');
    const resultDiv = document.getElementById('install-result');
    const allBtn = document.getElementById('install-all-btn');
    
    // 先获取当前状态
    let currentStatus;
    try {
        const statusResponse = await fetch('/api/admin/tokenizers_status');
        currentStatus = await statusResponse.json();
    } catch (e) {
        showMessage('danger', '无法获取当前分词器状态');
        return;
    }
    
    // 找出未安装的包
    const packagesToInstall = [];
    const statusMap = {
        'tiktoken': currentStatus.tiktoken?.available,
        'anthropic': currentStatus.anthropic?.available,
        'transformers': currentStatus.transformers?.available,
        'google-generativeai': currentStatus.google_generativeai?.available
    };
    
    for (const pkg of packages) {
        if (!statusMap[pkg]) {
            packagesToInstall.push(pkg);
        }
    }
    
    if (packagesToInstall.length === 0) {
        showMessage('success', '所有分词器都已安装！');
        return;
    }
    
    // 禁用按钮
    if (allBtn) {
        allBtn.disabled = true;
        allBtn.innerHTML = '安装中...';
    }
    
    // 显示进度
    progressDiv.style.display = 'block';
    resultDiv.style.display = 'none';
    
    const results = [];
    
    for (let i = 0; i < packagesToInstall.length; i++) {
        const pkg = packagesToInstall[i];
        progressText.textContent = `正在安装 ${pkg} (${i + 1}/${packagesToInstall.length})...`;
        
        try {
            const response = await fetch('/api/admin/install_tokenizer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ package: pkg })
            });
            
            const result = await response.json();
            results.push({ package: pkg, success: result.success, error: result.error });
        } catch (error) {
            results.push({ package: pkg, success: false, error: error.message });
        }
    }
    
    progressDiv.style.display = 'none';
    
    // 显示结果
    const successCount = results.filter(r => r.success).length;
    const failCount = results.filter(r => !r.success).length;
    
    let resultHtml = '<div style="padding: 12px; border-radius: 6px;';
    if (failCount === 0) {
        resultHtml += ' background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3);">';
        resultHtml += `<div style="color: #10b981; margin-bottom: 10px;">✅ 全部安装成功！(${successCount}/${packagesToInstall.length})</div>`;
    } else if (successCount === 0) {
        resultHtml += ' background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3);">';
        resultHtml += `<div style="color: #ef4444; margin-bottom: 10px;">❌ 全部安装失败</div>`;
    } else {
        resultHtml += ' background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.3);">';
        resultHtml += `<div style="color: #f59e0b; margin-bottom: 10px;">⚠️ 部分安装成功 (${successCount}/${packagesToInstall.length})</div>`;
    }
    
    resultHtml += '<ul style="margin: 0; padding-left: 20px; font-size: 0.85rem;">';
    for (const r of results) {
        if (r.success) {
            resultHtml += `<li style="color: #10b981;">${r.package}: 成功</li>`;
        } else {
            resultHtml += `<li style="color: #ef4444;">${r.package}: 失败 - ${r.error}</li>`;
        }
    }
    resultHtml += '</ul></div>';
    
    resultDiv.innerHTML = resultHtml;
    resultDiv.style.display = 'block';
    
    // 恢复按钮
    if (allBtn) {
        allBtn.disabled = false;
        allBtn.innerHTML = '安装全部';
    }
    
    // 刷新状态
    setTimeout(() => {
        refreshTokenizerInfo();
    }, 1000);
}

// 复制到剪贴板
function copyToClipboard(text) {
    navigator.clipboard.writeText(text).then(() => {
        showQuietMessage('success', '已复制到剪贴板');
    }).catch(err => {
        // 降级方案
        const textarea = document.createElement('textarea');
        textarea.value = text;
        textarea.style.position = 'fixed';
        textarea.style.left = '-9999px';
        document.body.appendChild(textarea);
        textarea.select();
        try {
            document.execCommand('copy');
            showQuietMessage('success', '已复制到剪贴板');
        } catch (e) {
            showMessage('danger', '复制失败');
        }
        document.body.removeChild(textarea);
    });
}

async function loadTokenizerMappings() {
    try {
        // 同时获取模型列表、分词器配置和自定义分词器列表
        const [modelsResponse, tokenizerResponse, customTokenizersResponse] = await Promise.all([
            fetch('/api/admin/models'),
            fetch('/api/admin/tokenizer_mappings'),
            fetch('/api/admin/custom_tokenizers')
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
        
        // 获取自定义分词器列表
        let customTokenizers = [];
        if (customTokenizersResponse.ok) {
            const customData = await customTokenizersResponse.json();
            customTokenizers = customData.tokenizers || [];
        }
        
        const container = document.getElementById('tokenizer-mappings-list');
        const modelEndpointMap = modelsData.model_endpoint_map;
        
        if (!modelEndpointMap || Object.keys(modelEndpointMap).length === 0) {
            container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🤖</div><p>还没有配置任何模型<br/>请先在"模型端点"页面添加模型</p></div>';
            return;
        }
        
        // 构建自定义分词器的选项HTML
        const customTokenizerOptions = customTokenizers
            .filter(t => t.available)
            .map(t => `<option value="custom_${t.name}">🔧 ${t.display_name || t.name}</option>`)
            .join('');
        
        // 构建自定义分词器选项组（如果有可用的自定义分词器）
        const customOptgroup = customTokenizers.filter(t => t.available).length > 0
            ? `<optgroup label="── 自定义分词器 ──">${customTokenizerOptions}</optgroup>`
            : '';
        
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
                        const isCustomTokenizer = currentTokenizer.startsWith('custom_');
                        
                        // 获取自定义分词器的显示名称
                        let tokenizerDisplayName = currentTokenizer;
                        if (isCustomTokenizer) {
                            const customName = currentTokenizer.replace('custom_', '');
                            const customTok = customTokenizers.find(t => t.name === customName);
                            if (customTok) {
                                tokenizerDisplayName = `🔧 ${customTok.display_name || customName}`;
                            }
                        }
                        
                        return `
                            <tr>
                                <td><strong>${modelName}</strong></td>
                                <td>
                                    <span class="badge ${
                                        currentTokenizer === 'anthropic' ? 'badge-info' :
                                        currentTokenizer === 'google' ? 'badge-success' :
                                        currentTokenizer === 'tiktoken' ? 'badge-info' :
                                        currentTokenizer === 'deepseek' ? 'badge-success' :
                                        isCustomTokenizer ? 'badge-success' :
                                        'badge-danger'
                                    }">${tokenizerDisplayName}</span>
                                    ${!hasCustomConfig ? '<span class="badge badge-info" style="margin-left: 5px;">默认</span>' : ''}
                                </td>
                                <td>
                                    <select class="form-select" data-model="${modelName}" style="width: auto; display: inline-block;">
                                        <optgroup label="── 内置分词器 ──">
                                            <option value="tiktoken" ${currentTokenizer === 'tiktoken' ? 'selected' : ''}>tiktoken (GPT)</option>
                                            <option value="anthropic" ${currentTokenizer === 'anthropic' ? 'selected' : ''}>anthropic (Claude)</option>
                                            <option value="google" ${currentTokenizer === 'google' ? 'selected' : ''}>google (Gemini)</option>
                                            <option value="deepseek" ${currentTokenizer === 'deepseek' ? 'selected' : ''}>deepseek</option>
                                            <option value="estimate" ${currentTokenizer === 'estimate' ? 'selected' : ''}>estimate (估算)</option>
                                        </optgroup>
                                        ${customOptgroup ? customOptgroup.replace(
                                            new RegExp(`value="(custom_[^"]+)"`, 'g'),
                                            (match, value) => `value="${value}" ${currentTokenizer === value ? 'selected' : ''}`
                                        ) : ''}
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
            ${customTokenizers.length === 0 ? `
                <div style="margin-top: 15px; padding: 12px; background: rgba(245, 158, 11, 0.1); border-left: 3px solid #f59e0b; border-radius: 0 4px 4px 0;">
                    <strong style="color: #f59e0b; font-size: 0.85rem;">💡 提示</strong>
                    <p style="font-size: 0.8rem; color: var(--text-dim); margin-top: 5px;">
                        还没有自定义分词器。可以在上方"自定义分词器管理"中添加GLM-4、Qwen、Kimi K2等模型的分词器。
                    </p>
                </div>
            ` : ''}
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

// ==================== 自定义分词器管理 ====================

// 加载自定义分词器列表
async function loadCustomTokenizers() {
    try {
        const response = await fetch('/api/admin/custom_tokenizers');
        
        if (!response.ok) {
            throw new Error(`HTTP ${response.status}`);
        }
        
        const data = await response.json();
        renderCustomTokenizersList(data);
        
    } catch (error) {
        console.error('❌ 加载自定义分词器失败:', error);
        const container = document.getElementById('custom-tokenizers-list');
        if (container) {
            container.innerHTML = `
                <div style="padding: 15px; text-align: center; color: var(--text-dim);">
                    加载失败: ${error.message}
                </div>
            `;
        }
    }
}

// 渲染自定义分词器列表
function renderCustomTokenizersList(data) {
    const container = document.getElementById('custom-tokenizers-list');
    if (!container) return;
    
    if (!data.tokenizers || data.tokenizers.length === 0) {
        container.innerHTML = `
            <div style="padding: 20px; text-align: center; color: var(--text-dim);">
                <div style="font-size: 2rem; margin-bottom: 10px;">📦</div>
                <p>还没有添加自定义分词器</p>
                <p style="font-size: 0.85rem;">点击上方"添加自定义分词器"按钮来添加</p>
            </div>
        `;
        return;
    }
    
    let html = `
        <table class="table">
            <thead>
                <tr>
                    <th>名称</th>
                    <th>来源</th>
                    <th>状态</th>
                    <th>支持模型</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody>
    `;
    
    for (const tokenizer of data.tokenizers) {
        const statusBadge = tokenizer.available
            ? '<span class="badge badge-success">✓ 可用</span>'
            : '<span class="badge badge-danger">✗ 不可用</span>';
        
        const sourceTypeText = tokenizer.source_type === 'huggingface' ? '🤗 HuggingFace' : '📁 本地';
        
        html += `
            <tr>
                <td>
                    <strong>${tokenizer.display_name || tokenizer.name}</strong>
                    <div style="font-size: 0.75rem; color: var(--text-dim);">${tokenizer.description || ''}</div>
                </td>
                <td>
                    <div style="font-size: 0.85rem;">${sourceTypeText}</div>
                    <code style="font-size: 0.7rem; word-break: break-all;">${tokenizer.source}</code>
                </td>
                <td>${statusBadge}</td>
                <td style="font-size: 0.8rem; color: var(--text-dim);">
                    ${(tokenizer.supported_models || []).join(', ') || '-'}
                </td>
                <td>
                    <button class="btn btn-danger btn-sm" onclick="deleteCustomTokenizer('${tokenizer.name}')">
                        🗑️ 删除
                    </button>
                </td>
            </tr>
        `;
    }
    
    html += `
            </tbody>
        </table>
    `;
    
    container.innerHTML = html;
}

// 显示添加自定义分词器对话框
function showAddCustomTokenizerDialog() {
    // 创建模态框
    const modal = document.createElement('div');
    modal.id = 'add-custom-tokenizer-modal';
    modal.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.7);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 10000;
    `;
    
    modal.innerHTML = `
        <div style="background: var(--surface); border: 1px solid var(--line-strong); border-radius: 12px; width: 90%; max-width: 600px; max-height: 90vh; overflow-y: auto;">
            <div style="padding: 20px; border-bottom: 1px solid var(--line-weak);">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin: 0; color: var(--text-main);">➕ 添加自定义分词器</h3>
                    <button onclick="closeCustomTokenizerDialog()" style="background: none; border: none; font-size: 1.5rem; cursor: pointer; color: var(--text-dim);">×</button>
                </div>
            </div>
            
            <div style="padding: 20px;">
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        分词器名称 <span style="color: #ef4444;">*</span>
                    </label>
                    <input type="text" id="custom-tokenizer-name" class="form-input"
                           placeholder="例如: glm4, kimi-k2, qwen2"
                           style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);">
                    <div style="font-size: 0.75rem; color: var(--text-dim); margin-top: 5px;">
                        只能包含字母、数字、下划线和横杠
                    </div>
                </div>
                
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        显示名称
                    </label>
                    <input type="text" id="custom-tokenizer-display-name" class="form-input"
                           placeholder="例如: GLM-4 Tokenizer"
                           style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);">
                </div>
                
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        来源类型 <span style="color: #ef4444;">*</span>
                    </label>
                    <select id="custom-tokenizer-source-type" class="form-select" style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);" onchange="toggleSourceHelp()">
                        <option value="huggingface">🤗 HuggingFace 模型</option>
                        <option value="local">📁 本地路径 (transformers格式)</option>
                        <option value="tiktoken_model">🔤 Tiktoken Model (如Kimi K2)</option>
                    </select>
                </div>
                
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        来源地址 <span style="color: #ef4444;">*</span>
                    </label>
                    <input type="text" id="custom-tokenizer-source" class="form-input"
                           placeholder="例如: THUDM/glm-4-9b-chat"
                           style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);">
                    <div id="source-help" style="font-size: 0.75rem; color: var(--text-dim); margin-top: 5px;">
                        输入HuggingFace模型ID，例如: THUDM/glm-4-9b-chat, moonshot-ai/Kimi-K2-Instruct
                    </div>
                </div>
                
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        描述
                    </label>
                    <input type="text" id="custom-tokenizer-description" class="form-input"
                           placeholder="例如: 智谱GLM-4系列模型分词器"
                           style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);">
                </div>
                
                <div style="margin-bottom: 20px;">
                    <label style="display: block; margin-bottom: 8px; color: var(--text-main); font-weight: 500;">
                        支持的模型（可选）
                    </label>
                    <input type="text" id="custom-tokenizer-models" class="form-input"
                           placeholder="用逗号分隔，例如: glm-4, glm-4-plus, glm-4-air"
                           style="width: 100%; padding: 10px; border: 1px solid var(--line-strong); border-radius: 6px; background: var(--surface-2); color: var(--text-main);">
                    <div style="font-size: 0.75rem; color: var(--text-dim); margin-top: 5px;">
                        这些模型名称会在模型映射中显示此分词器
                    </div>
                </div>
                
                <div style="padding: 15px; background: rgba(245, 158, 11, 0.1); border-left: 3px solid #f59e0b; border-radius: 0 4px 4px 0; margin-bottom: 20px;">
                    <strong style="color: #f59e0b; font-size: 0.85rem;">⚠️ 注意事项</strong>
                    <ul style="font-size: 0.8rem; color: var(--text-dim); margin: 8px 0 0 0; padding-left: 20px;">
                        <li>首次添加HuggingFace分词器时会下载模型文件</li>
                        <li>某些模型可能需要较长时间下载</li>
                        <li>需要安装 <code>transformers</code> 库</li>
                        <li>部分模型需要登录HuggingFace账号</li>
                    </ul>
                </div>
                
                <div id="add-tokenizer-progress" style="display: none; margin-bottom: 15px;">
                    <div style="display: flex; align-items: center; gap: 10px; padding: 12px; background: var(--surface-2); border-radius: 6px;">
                        <div class="loading-spinner" style="width: 20px; height: 20px; border: 2px solid var(--line-strong); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite;"></div>
                        <span id="add-tokenizer-progress-text" style="color: var(--text-dim);">正在添加...</span>
                    </div>
                </div>
                
                <div id="add-tokenizer-result" style="display: none; margin-bottom: 15px;"></div>
            </div>
            
            <div style="padding: 15px 20px; border-top: 1px solid var(--line-weak); display: flex; justify-content: flex-end; gap: 10px;">
                <button class="btn" onclick="closeCustomTokenizerDialog()">取消</button>
                <button class="btn btn-primary" onclick="submitCustomTokenizer()" id="submit-custom-tokenizer-btn">
                    添加分词器
                </button>
            </div>
        </div>
    `;
    
    document.body.appendChild(modal);
    
    // 点击背景关闭
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeCustomTokenizerDialog();
        }
    });
}

// 切换来源帮助提示
function toggleSourceHelp() {
    const sourceType = document.getElementById('custom-tokenizer-source-type').value;
    const sourceInput = document.getElementById('custom-tokenizer-source');
    const helpDiv = document.getElementById('source-help');
    
    if (sourceType === 'huggingface') {
        sourceInput.placeholder = '例如: THUDM/glm-4-9b-chat';
        helpDiv.innerHTML = '输入HuggingFace模型ID，例如: THUDM/glm-4-9b-chat, Qwen/Qwen2-7B-Instruct';
    } else if (sourceType === 'tiktoken_model') {
        sourceInput.placeholder = '例如: ./tokenizers/kimi-k2';
        helpDiv.innerHTML = `
            输入包含<code style="background: var(--surface); padding: 2px 6px; border-radius: 3px;">tiktoken.model</code>文件的目录路径<br>
            <span style="color: #f59e0b;">适用于Kimi K2等使用tiktoken格式的模型</span>
        `;
    } else {
        sourceInput.placeholder = '例如: ./tokenizers/my-tokenizer';
        helpDiv.innerHTML = '输入本地tokenizer目录路径，需要包含<code style="background: var(--surface); padding: 2px 6px; border-radius: 3px;">tokenizer.json</code>等文件';
    }
}

// 关闭对话框
function closeCustomTokenizerDialog() {
    const modal = document.getElementById('add-custom-tokenizer-modal');
    if (modal) {
        modal.remove();
    }
}

// 提交添加自定义分词器
async function submitCustomTokenizer() {
    const name = document.getElementById('custom-tokenizer-name').value.trim();
    const displayName = document.getElementById('custom-tokenizer-display-name').value.trim();
    const sourceType = document.getElementById('custom-tokenizer-source-type').value;
    const source = document.getElementById('custom-tokenizer-source').value.trim();
    const description = document.getElementById('custom-tokenizer-description').value.trim();
    const modelsStr = document.getElementById('custom-tokenizer-models').value.trim();
    
    // 验证必填字段
    if (!name) {
        showMessage('danger', '请输入分词器名称');
        return;
    }
    
    if (!source) {
        showMessage('danger', '请输入来源地址');
        return;
    }
    
    // 验证名称格式
    if (!/^[a-zA-Z0-9_-]+$/.test(name)) {
        showMessage('danger', '名称只能包含字母、数字、下划线和横杠');
        return;
    }
    
    // 解析支持的模型
    const supportedModels = modelsStr
        ? modelsStr.split(',').map(m => m.trim()).filter(m => m)
        : [];
    
    const progressDiv = document.getElementById('add-tokenizer-progress');
    const progressText = document.getElementById('add-tokenizer-progress-text');
    const resultDiv = document.getElementById('add-tokenizer-result');
    const submitBtn = document.getElementById('submit-custom-tokenizer-btn');
    
    // 显示进度
    progressDiv.style.display = 'block';
    if (sourceType === 'huggingface') {
        progressText.textContent = `正在从HuggingFace下载 ${source}...（可能需要几分钟）`;
    } else if (sourceType === 'tiktoken_model') {
        progressText.textContent = `正在加载tiktoken.model分词器 ${source}...`;
    } else {
        progressText.textContent = `正在加载本地分词器 ${source}...`;
    }
    resultDiv.style.display = 'none';
    submitBtn.disabled = true;
    submitBtn.innerHTML = '添加中...';
    
    try {
        const response = await fetch('/api/admin/custom_tokenizers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: name,
                display_name: displayName || name,
                source_type: sourceType,
                source: source,
                description: description,
                supported_models: supportedModels
            })
        });
        
        const result = await response.json();
        
        progressDiv.style.display = 'none';
        
        if (result.success) {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; color: #10b981;">
                    ✅ ${result.message}
                    <div style="font-size: 0.85rem; margin-top: 5px; color: var(--text-dim);">
                        ${result.test_result || ''}
                    </div>
                </div>
            `;
            showMessage('success', `分词器 ${displayName || name} 添加成功！`);
            
            // 刷新列表
            setTimeout(() => {
                closeCustomTokenizerDialog();
                loadCustomTokenizers();
                refreshTokenizerInfo();
            }, 1500);
            
        } else {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                    ❌ 添加失败: ${result.error || '未知错误'}
                </div>
            `;
            submitBtn.disabled = false;
            submitBtn.innerHTML = '添加分词器';
        }
        
    } catch (error) {
        progressDiv.style.display = 'none';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `
            <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                ❌ 网络错误: ${error.message}
            </div>
        `;
        submitBtn.disabled = false;
        submitBtn.innerHTML = '添加分词器';
    }
}

// 删除自定义分词器
async function deleteCustomTokenizer(name) {
    if (!confirm(`确定要删除分词器 "${name}" 吗？`)) {
        return;
    }
    
    try {
        const response = await fetch(`/api/admin/custom_tokenizers/${encodeURIComponent(name)}`, {
            method: 'DELETE'
        });
        
        const result = await response.json();
        
        if (result.success) {
            showMessage('success', `分词器 ${name} 已删除`);
            loadCustomTokenizers();
            refreshTokenizerInfo();
        } else {
            showMessage('danger', `删除失败: ${result.error}`);
        }
        
    } catch (error) {
        showMessage('danger', `网络错误: ${error.message}`);
    }
}

// 常用分词器快速添加
const POPULAR_TOKENIZERS = [
    {
        name: 'glm4',
        display_name: 'GLM-4',
        source: 'THUDM/glm-4-9b-chat',
        source_type: 'huggingface',
        description: '智谱GLM-4系列模型分词器',
        models: ['glm-4', 'glm-4-plus', 'glm-4-air', 'glm-4-flash']
    },
    {
        name: 'qwen2',
        display_name: 'Qwen2',
        source: 'Qwen/Qwen2-7B-Instruct',
        source_type: 'huggingface',
        description: '通义千问Qwen2系列模型分词器',
        models: ['qwen-2', 'qwen-2-72b', 'qwen-2.5']
    },
    {
        name: 'kimi-k2',
        display_name: 'Kimi K2',
        source: 'moonshotai/Kimi-K2-Instruct',
        source_type: 'huggingface',  // Kimi K2虽然使用tiktoken.model，但HuggingFace上有transformers格式
        description: 'Moonshot Kimi K2系列模型分词器',
        models: ['kimi-k2', 'kimi-k2-0711', 'moonshot-v1']
    },
    {
        name: 'yi',
        display_name: 'Yi',
        source: '01-ai/Yi-6B',
        source_type: 'huggingface',
        description: '零一万物Yi系列模型分词器',
        models: ['yi-34b', 'yi-6b', 'yi-lightning']
    },
    {
        name: 'llama3',
        display_name: 'Llama 3',
        source: 'meta-llama/Meta-Llama-3-8B',
        source_type: 'huggingface',
        description: 'Meta Llama 3系列模型分词器',
        models: ['llama-3', 'llama-3-70b', 'llama-3.1']
    },
    {
        name: 'mistral',
        display_name: 'Mistral',
        source: 'mistralai/Mistral-7B-Instruct-v0.3',
        source_type: 'huggingface',
        description: 'Mistral系列模型分词器',
        models: ['mistral', 'mistral-large', 'mixtral']
    }
];

// 显示快速添加对话框
function showQuickAddTokenizerDialog() {
    const modal = document.createElement('div');
    modal.id = 'quick-add-tokenizer-modal';
    modal.style.cssText = `
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.7);
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 10000;
    `;
    
    let cardsHtml = '';
    for (const tok of POPULAR_TOKENIZERS) {
        const sourceTypeLabel = tok.source_type === 'tiktoken_model' ? '🔤 tiktoken' :
                                tok.source_type === 'local' ? '📁 本地' : '🤗 HuggingFace';
        cardsHtml += `
            <div style="padding: 15px; background: var(--surface-2); border: 1px solid var(--line-weak); border-radius: 8px;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 10px;">
                    <div>
                        <strong style="color: var(--text-main);">${tok.display_name}</strong>
                        <span style="font-size: 0.65rem; color: var(--text-dim); margin-left: 8px;">${sourceTypeLabel}</span>
                        <div style="font-size: 0.75rem; color: var(--text-dim);">${tok.description}</div>
                    </div>
                </div>
                <code style="font-size: 0.7rem; color: var(--accent); display: block; margin-bottom: 10px; word-break: break-all;">
                    ${tok.source}
                </code>
                <div style="font-size: 0.7rem; color: var(--text-dim); margin-bottom: 10px;">
                    模型: ${tok.models.join(', ')}
                </div>
                <button class="btn btn-primary btn-sm" onclick="quickAddTokenizer('${tok.name}', '${tok.display_name}', '${tok.source}', '${tok.description}', '${tok.models.join(',')}', '${tok.source_type || 'huggingface'}')">
                    快速添加
                </button>
            </div>
        `;
    }
    
    modal.innerHTML = `
        <div style="background: var(--surface); border: 1px solid var(--line-strong); border-radius: 12px; width: 90%; max-width: 700px; max-height: 90vh; overflow-y: auto;">
            <div style="padding: 20px; border-bottom: 1px solid var(--line-weak);">
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <h3 style="margin: 0; color: var(--text-main);">⚡ 快速添加常用分词器</h3>
                    <button onclick="closeQuickAddDialog()" style="background: none; border: none; font-size: 1.5rem; cursor: pointer; color: var(--text-dim);">×</button>
                </div>
                <p style="margin: 10px 0 0 0; font-size: 0.85rem; color: var(--text-dim);">
                    选择一个常用分词器快速添加（需要安装transformers库）
                </p>
            </div>
            
            <div style="padding: 20px;">
                <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 15px;">
                    ${cardsHtml}
                </div>
                
                <div id="quick-add-progress" style="display: none; margin-top: 20px;">
                    <div style="display: flex; align-items: center; gap: 10px; padding: 12px; background: var(--surface-2); border-radius: 6px;">
                        <div class="loading-spinner" style="width: 20px; height: 20px; border: 2px solid var(--line-strong); border-top-color: var(--accent); border-radius: 50%; animation: spin 1s linear infinite;"></div>
                        <span id="quick-add-progress-text" style="color: var(--text-dim);">正在添加...</span>
                    </div>
                </div>
                
                <div id="quick-add-result" style="display: none; margin-top: 15px;"></div>
            </div>
        </div>
    `;
    
    document.body.appendChild(modal);
    
    modal.addEventListener('click', (e) => {
        if (e.target === modal) {
            closeQuickAddDialog();
        }
    });
}

function closeQuickAddDialog() {
    const modal = document.getElementById('quick-add-tokenizer-modal');
    if (modal) {
        modal.remove();
    }
}

// 快速添加分词器
async function quickAddTokenizer(name, displayName, source, description, modelsStr, sourceType = 'huggingface') {
    const progressDiv = document.getElementById('quick-add-progress');
    const progressText = document.getElementById('quick-add-progress-text');
    const resultDiv = document.getElementById('quick-add-result');
    
    progressDiv.style.display = 'block';
    if (sourceType === 'tiktoken_model') {
        progressText.textContent = `正在加载tiktoken.model分词器 ${source}...`;
    } else if (sourceType === 'local') {
        progressText.textContent = `正在加载本地分词器 ${source}...`;
    } else {
        progressText.textContent = `正在从HuggingFace下载 ${source}...（可能需要几分钟）`;
    }
    resultDiv.style.display = 'none';
    
    try {
        const response = await fetch('/api/admin/custom_tokenizers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name: name,
                display_name: displayName,
                source_type: sourceType,
                source: source,
                description: description,
                supported_models: modelsStr.split(',')
            })
        });
        
        const result = await response.json();
        
        progressDiv.style.display = 'none';
        
        if (result.success) {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(16, 185, 129, 0.1); border: 1px solid rgba(16, 185, 129, 0.3); border-radius: 6px; color: #10b981;">
                    ✅ ${displayName} 添加成功！${result.test_result ? '<br><small>' + result.test_result + '</small>' : ''}
                </div>
            `;
            showMessage('success', `${displayName} 添加成功！`);
            
            setTimeout(() => {
                closeQuickAddDialog();
                loadCustomTokenizers();
                refreshTokenizerInfo();
            }, 1500);
            
        } else {
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = `
                <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                    ❌ 添加失败: ${result.error || '未知错误'}
                </div>
            `;
        }
        
    } catch (error) {
        progressDiv.style.display = 'none';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = `
            <div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 6px; color: #ef4444;">
                ❌ 网络错误: ${error.message}
            </div>
        `;
    }
}