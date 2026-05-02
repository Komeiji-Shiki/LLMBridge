// admin-config.js - 配置编辑功能

// ==================== 配置编辑模式切换 ====================
function switchConfigMode(mode) {
    currentConfigMode = mode;
    
    // 更新按钮状态
    document.getElementById('mode-jsonc-btn').className = mode === 'jsonc' ? 'btn btn-primary' : 'btn';
    document.getElementById('mode-form-btn').className = mode === 'form' ? 'btn btn-primary' : 'btn';
    
    // 切换编辑器显示
    if (mode === 'jsonc') {
        document.getElementById('config-jsonc-editor').style.display = 'block';
        document.getElementById('config-form-editor').style.display = 'none';
    } else {
        // 从JSONC转换到表单模式时，先解析当前内容
        try {
            const jsonContent = document.getElementById('config-editor').value;
            currentConfigData = parseJsonc(jsonContent);
            buildConfigForm();
            document.getElementById('config-jsonc-editor').style.display = 'none';
            document.getElementById('config-form-editor').style.display = 'block';
        } catch (error) {
            showConfigMessage('danger', '解析配置失败: ' + error.message);
            // 切换回JSONC模式
            currentConfigMode = 'jsonc';
            document.getElementById('mode-jsonc-btn').className = 'btn btn-primary';
            document.getElementById('mode-form-btn').className = 'btn';
        }
    }
}

// ==================== 构建配置表单 ====================
function buildConfigForm() {
    const container = document.getElementById('config-form-content');
    if (!currentConfigData) {
        container.innerHTML = '<p style="color: var(--text-dim);">无法加载配置数据</p>';
        return;
    }
    
    let html = '';
    
    // 基础配置
    html += buildBasicConfig();
    
    // ID 更新器配置
    html += buildIdUpdaterConfig();
    
    // 重试配置
    html += buildRetryConfig();
    
    // Bypass配置
    html += buildBypassConfig();
    
    // 图像配置
    html += buildImageConfig();
    
    // 消息转换配置
    html += buildMessageConfig();
    
    // 图床配置
    html += buildFileBedConfig();
    
    // 连接和性能配置
    html += buildPerformanceConfig();
    
    // Reasoning配置
    html += buildReasoningConfig();
    
    // 其他设置
    html += buildOtherConfig();
    
    container.innerHTML = html;
}

// ==================== 配置卡片构建函数 ====================

// 辅助函数：创建可折叠配置区块
function buildCollapsibleSection(title, content, collapsed = true) {
    return `
        <div class="collapsible-section ${collapsed ? 'collapsed' : ''}" style="margin-bottom: 10px;">
            <div class="collapsible-header" onclick="toggleCollapsible(this)">
                <h4>${title}</h4>
                <span class="collapsible-toggle">▼</span>
            </div>
            <div class="collapsible-content">
                ${content}
            </div>
        </div>
    `;
}

// 辅助函数：创建双列表单组
function buildFormRow(...items) {
    return `<div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px 15px;">${items.join('')}</div>`;
}

// 辅助函数：紧凑表单项
function buildCompactFormGroup(label, inputHtml, hint = '') {
    return `
        <div class="form-group" style="margin-bottom: 10px;">
            <label class="form-label" style="margin-bottom: 4px; font-size: 0.8rem;">${label}</label>
            ${inputHtml}
            ${hint ? `<small style="color: var(--text-dim); font-size: 0.7rem;">${hint}</small>` : ''}
        </div>
    `;
}

function buildBasicConfig() {
    const content = buildFormRow(
        buildCompactFormGroup('版本号',
            `<input type="text" class="form-input" id="form-version" value="${currentConfigData.version || ''}" readonly style="padding: 6px 8px; font-size: 0.85rem;">`,
            '请不要手动修改'),
        buildCompactFormGroup('服务器端口号',
            `<input type="number" class="form-input" id="form-server_port" value="${currentConfigData.server_port || 5102}" min="1" max="65535" style="padding: 6px 8px; font-size: 0.85rem;">`,
            '修改后需重启'),
        buildCompactFormGroup('Session ID',
            `<input type="text" class="form-input" id="form-session_id" value="${currentConfigData.session_id || ''}" style="padding: 6px 8px; font-size: 0.85rem;">`,
            'LMArena 页面的会话 ID')
    );
    return buildCollapsibleSection('📋 基础配置', content, false); // 基础配置默认展开
}

function buildIdUpdaterConfig() {
    const content = buildFormRow(
        buildCompactFormGroup('上次使用的模式',
            `<select class="form-select" id="form-id_updater_last_mode" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="direct_chat" ${currentConfigData.id_updater_last_mode === 'direct_chat' ? 'selected' : ''}>Direct Chat</option>
                <option value="battle" ${currentConfigData.id_updater_last_mode === 'battle' ? 'selected' : ''}>Battle</option>
            </select>`),
        buildCompactFormGroup('Battle 模式目标',
            `<select class="form-select" id="form-id_updater_battle_target" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="A" ${currentConfigData.id_updater_battle_target === 'A' ? 'selected' : ''}>A</option>
                <option value="B" ${currentConfigData.id_updater_battle_target === 'B' ? 'selected' : ''}>B</option>
            </select>`),
        buildCompactFormGroup('自动保存模式',
            `<select class="form-select" id="form-id_updater_auto_save_mode" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="model" ${currentConfigData.id_updater_auto_save_mode === 'model' ? 'selected' : ''}>model (推荐)</option>
                <option value="global" ${currentConfigData.id_updater_auto_save_mode === 'global' ? 'selected' : ''}>global</option>
                <option value="ask" ${currentConfigData.id_updater_auto_save_mode === 'ask' ? 'selected' : ''}>ask</option>
            </select>`)
    );
    return buildCollapsibleSection('🎯 ID 更新器配置', content);
}

function buildRetryConfig() {
    const retryConfig = currentConfigData.empty_response_retry || {};
    const content = `
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px 15px;">
            <label style="display: flex; align-items: center; font-size: 0.85rem;">
                <input type="checkbox" id="form-enable_auto_retry" ${currentConfigData.enable_auto_retry ? 'checked' : ''} style="margin-right: 6px;">
                启用自动重试
            </label>
            <label style="display: flex; align-items: center; font-size: 0.85rem;">
                <input type="checkbox" id="form-empty_response_retry_enabled" ${retryConfig.enabled ? 'checked' : ''} style="margin-right: 6px;">
                启用空响应重试
            </label>
            ${buildCompactFormGroup('重试超时(秒)',
                `<input type="number" class="form-input" id="form-retry_timeout_seconds" value="${currentConfigData.retry_timeout_seconds || 60}" style="padding: 6px 8px; font-size: 0.85rem;">`)}
            ${buildCompactFormGroup('最大重试次数',
                `<input type="number" class="form-input" id="form-empty_response_retry_max_retries" value="${retryConfig.max_retries || 5}" style="padding: 6px 8px; font-size: 0.85rem;">`)}
            ${buildCompactFormGroup('基础延迟(ms)',
                `<input type="number" class="form-input" id="form-empty_response_retry_base_delay_ms" value="${retryConfig.base_delay_ms || 100}" style="padding: 6px 8px; font-size: 0.85rem;">`)}
            ${buildCompactFormGroup('最大延迟(ms)',
                `<input type="number" class="form-input" id="form-empty_response_retry_max_delay_ms" value="${retryConfig.max_delay_ms || 3000}" style="padding: 6px 8px; font-size: 0.85rem;">`)}
        </div>
        <label style="display: flex; align-items: center; font-size: 0.85rem; margin-top: 8px;">
            <input type="checkbox" id="form-empty_response_retry_show_retry_info_to_client" ${retryConfig.show_retry_info_to_client ? 'checked' : ''} style="margin-right: 6px;">
            向客户端显示重试信息
        </label>
    `;
    return buildCollapsibleSection('🔄 重试配置', content);
}

function buildBypassConfig() {
    const bypassSettings = currentConfigData.bypass_settings || {};
    const attachmentBypassSettings = currentConfigData.attachment_bypass_settings || {};
    const content = `
        <label style="display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 10px;">
            <input type="checkbox" id="form-bypass_enabled" ${currentConfigData.bypass_enabled ? 'checked' : ''} style="margin-right: 6px;">
            <strong>启用 Bypass 模式</strong>
        </label>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">Bypass 设置</div>
                <div style="display: flex; gap: 12px; flex-wrap: wrap;">
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-bypass_settings_text" ${bypassSettings.text ? 'checked' : ''} style="margin-right: 4px;">文本</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-bypass_settings_search" ${bypassSettings.search ? 'checked' : ''} style="margin-right: 4px;">搜索</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-bypass_settings_image" ${bypassSettings.image ? 'checked' : ''} style="margin-right: 4px;">图像</label>
                </div>
            </div>
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">附件 Bypass</div>
                <div style="display: flex; gap: 12px; flex-wrap: wrap;">
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-attachment_bypass_settings_text" ${attachmentBypassSettings.text ? 'checked' : ''} style="margin-right: 4px;">文本</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-attachment_bypass_settings_search" ${attachmentBypassSettings.search ? 'checked' : ''} style="margin-right: 4px;">搜索</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-attachment_bypass_settings_image" ${attachmentBypassSettings.image ? 'checked' : ''} style="margin-right: 4px;">图像</label>
                </div>
            </div>
        </div>
    `;
    return buildCollapsibleSection('🚀 Bypass 配置', content);
}

function buildImageConfig() {
    const localSaveFormat = currentConfigData.local_save_format || {};
    const imageReturnFormat = currentConfigData.image_return_format || {};
    const base64Conversion = imageReturnFormat.base64_conversion || {};
    const imageOptimization = currentConfigData.image_optimization || {};
    const processedImageCache = currentConfigData.processed_image_cache || {};
    
    const content = `
        <label style="display: flex; align-items: center; font-size: 0.85rem; margin-bottom: 10px;">
            <input type="checkbox" id="form-save_images_locally" ${currentConfigData.save_images_locally ? 'checked' : ''} style="margin-right: 6px;">
            <strong>本地保存图像</strong>
        </label>
        
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
            <!-- 本地保存格式 -->
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">本地保存格式</div>
                <label style="font-size: 0.8rem;"><input type="checkbox" id="form-local_save_format_enabled" ${localSaveFormat.enabled ? 'checked' : ''} style="margin-right: 4px;">启用格式转换</label>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 6px;">
                    ${buildCompactFormGroup('格式', `<select class="form-select" id="form-local_save_format_format" style="padding: 4px; font-size: 0.8rem;">
                        <option value="png" ${localSaveFormat.format === 'png' ? 'selected' : ''}>PNG</option>
                        <option value="jpeg" ${localSaveFormat.format === 'jpeg' ? 'selected' : ''}>JPEG</option>
                        <option value="webp" ${localSaveFormat.format === 'webp' ? 'selected' : ''}>WebP</option>
                    </select>`)}
                    ${buildCompactFormGroup('JPEG质量', `<input type="number" class="form-input" id="form-local_save_format_jpeg_quality" value="${localSaveFormat.jpeg_quality || 100}" min="1" max="100" style="padding: 4px; font-size: 0.8rem;">`)}
                </div>
            </div>
            
            <!-- 返回格式 -->
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">返回格式</div>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px;">
                    ${buildCompactFormGroup('模式', `<select class="form-select" id="form-image_return_format_mode" style="padding: 4px; font-size: 0.8rem;">
                        <option value="base64" ${imageReturnFormat.mode === 'base64' ? 'selected' : ''}>Base64</option>
                        <option value="url" ${imageReturnFormat.mode === 'url' ? 'selected' : ''}>URL</option>
                    </select>`)}
                    ${buildCompactFormGroup('目标格式', `<select class="form-select" id="form-image_return_format_base64_conversion_target_format" style="padding: 4px; font-size: 0.8rem;">
                        <option value="png" ${base64Conversion.target_format === 'png' ? 'selected' : ''}>PNG</option>
                        <option value="jpeg" ${base64Conversion.target_format === 'jpeg' ? 'selected' : ''}>JPEG</option>
                        <option value="webp" ${base64Conversion.target_format === 'webp' ? 'selected' : ''}>WebP</option>
                    </select>`)}
                </div>
                <label style="font-size: 0.8rem; margin-top: 6px;"><input type="checkbox" id="form-image_return_format_base64_conversion_enabled" ${base64Conversion.enabled ? 'checked' : ''} style="margin-right: 4px;">启用Base64转换</label>
            </div>
            
            <!-- 图像优化 -->
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">图像优化</div>
                <div style="display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 6px;">
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-image_optimization_enabled" ${imageOptimization.enabled ? 'checked' : ''} style="margin-right: 4px;">启用</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-image_optimization_strip_metadata" ${imageOptimization.strip_metadata ? 'checked' : ''} style="margin-right: 4px;">移除元数据</label>
                    <label style="font-size: 0.8rem;"><input type="checkbox" id="form-image_optimization_convert_to_webp" ${imageOptimization.convert_to_webp ? 'checked' : ''} style="margin-right: 4px;">转WebP</label>
                </div>
                <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 6px;">
                    ${buildCompactFormGroup('WebP质量', `<input type="number" class="form-input" id="form-image_optimization_webp_quality" value="${imageOptimization.webp_quality || 70}" min="1" max="100" style="padding: 4px; font-size: 0.8rem;">`)}
                    ${buildCompactFormGroup('最大宽', `<input type="number" class="form-input" id="form-image_optimization_max_width" value="${imageOptimization.max_width || 4096}" style="padding: 4px; font-size: 0.8rem;">`)}
                    ${buildCompactFormGroup('最大高', `<input type="number" class="form-input" id="form-image_optimization_max_height" value="${imageOptimization.max_height || 4096}" style="padding: 4px; font-size: 0.8rem;">`)}
                </div>
            </div>
            
            <!-- 图像缓存 -->
            <div style="padding: 8px; background: var(--surface-2); border-radius: 4px;">
                <div style="font-size: 0.75rem; color: var(--text-dim); margin-bottom: 6px;">图像缓存</div>
                <label style="font-size: 0.8rem;"><input type="checkbox" id="form-processed_image_cache_enabled" ${processedImageCache.enabled ? 'checked' : ''} style="margin-right: 4px;">启用缓存</label>
                <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-top: 6px;">
                    ${buildCompactFormGroup('TTL(秒)', `<input type="number" class="form-input" id="form-processed_image_cache_ttl_seconds" value="${processedImageCache.ttl_seconds || 3600}" style="padding: 4px; font-size: 0.8rem;">`)}
                    ${buildCompactFormGroup('最大大小', `<input type="number" class="form-input" id="form-processed_image_cache_max_size" value="${processedImageCache.max_size || 200}" style="padding: 4px; font-size: 0.8rem;">`)}
                </div>
            </div>
        </div>
    `;
    return buildCollapsibleSection('🖼️ 图像处理配置', content);
}

function buildMessageConfig() {
    const content = `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start;">
            ${buildCompactFormGroup('消息角色转换模式', `<select class="form-select" id="form-message_role_conversion_mode" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="system_merge" ${currentConfigData.message_role_conversion_mode === 'system_merge' ? 'selected' : ''}>system_merge</option>
                <option value="preserve" ${currentConfigData.message_role_conversion_mode === 'preserve' ? 'selected' : ''}>preserve</option>
                <option value="convert" ${currentConfigData.message_role_conversion_mode === 'convert' ? 'selected' : ''}>convert</option>
            </select>`)}
            <div style="display: flex; flex-direction: column; gap: 6px; padding-top: 20px;">
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-merge_preserve_role_labels" ${currentConfigData.merge_preserve_role_labels ? 'checked' : ''} style="margin-right: 6px;">保留角色标签</label>
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-tavern_mode_enabled" ${currentConfigData.tavern_mode_enabled ? 'checked' : ''} style="margin-right: 6px;">Tavern 模式</label>
            </div>
        </div>
    `;
    return buildCollapsibleSection('💬 消息转换配置', content);
}

function buildFileBedConfig() {
    const content = `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start;">
            <label style="font-size: 0.85rem; padding-top: 6px;"><input type="checkbox" id="form-file_bed_enabled" ${currentConfigData.file_bed_enabled ? 'checked' : ''} style="margin-right: 6px;"><strong>启用图床上传</strong></label>
            ${buildCompactFormGroup('选择策略', `<select class="form-select" id="form-file_bed_selection_strategy" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="round_robin" ${currentConfigData.file_bed_selection_strategy === 'round_robin' ? 'selected' : ''}>round_robin</option>
                <option value="random" ${currentConfigData.file_bed_selection_strategy === 'random' ? 'selected' : ''}>random</option>
                <option value="priority" ${currentConfigData.file_bed_selection_strategy === 'priority' ? 'selected' : ''}>priority</option>
            </select>`)}
        </div>
        <small style="color: var(--text-dim); font-size: 0.75rem;">图床端点配置请使用 JSONC 模式编辑</small>
    `;
    return buildCollapsibleSection('📤 图床配置', content);
}

function buildPerformanceConfig() {
    const connectionPool = currentConfigData.connection_pool || {};
    const downloadTimeout = currentConfigData.download_timeout || {};
    const memoryManagement = currentConfigData.memory_management || {};
    const backgroundTasks = currentConfigData.background_tasks || {};
    const cacheSettings = currentConfigData.cache_settings || {};
    
    // 超时配置
    const timeoutContent = `
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;">
            ${buildCompactFormGroup('流响应(秒)', `<input type="number" class="form-input" id="form-stream_response_timeout_seconds" value="${currentConfigData.stream_response_timeout_seconds || 600}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('API调用(秒)', `<input type="number" class="form-input" id="form-api_call_timeout_seconds" value="${currentConfigData.api_call_timeout_seconds || 1200}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('首块(秒)', `<input type="number" class="form-input" id="form-first_chunk_timeout_seconds" value="${currentConfigData.first_chunk_timeout_seconds || 180}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('WebSocket(秒)', `<input type="number" class="form-input" id="form-websocket_send_timeout_seconds" value="${currentConfigData.websocket_send_timeout_seconds || 10}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('元数据(分)', `<input type="number" class="form-input" id="form-metadata_timeout_minutes" value="${currentConfigData.metadata_timeout_minutes || 30}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('活跃请求(分)', `<input type="number" class="form-input" id="form-active_request_timeout_minutes" value="${currentConfigData.active_request_timeout_minutes || 30}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('Tokenizer(秒)', `<input type="number" class="form-input" id="form-tokenizer_idle_timeout_seconds" value="${currentConfigData.tokenizer_idle_timeout_seconds || 600}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('负载锁(秒)', `<input type="number" class="form-input" id="form-load_balancer_lock_timeout_seconds" value="${currentConfigData.load_balancer_lock_timeout_seconds || 5}" step="0.1" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 服务器配置
    const serverContent = `
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px;">
            <label style="font-size: 0.85rem; display: flex; align-items: center;"><input type="checkbox" id="form-use_default_ids_if_mapping_not_found" ${currentConfigData.use_default_ids_if_mapping_not_found ? 'checked' : ''} style="margin-right: 6px;">映射未找到时用默认ID</label>
            <div></div>
            ${buildCompactFormGroup('验证冷却(秒)', `<input type="number" class="form-input" id="form-verification_cooldown_seconds" value="${currentConfigData.verification_cooldown_seconds || 25}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('流结束延迟(秒)', `<input type="number" class="form-input" id="form-stream_end_wait_delay_seconds" value="${currentConfigData.stream_end_wait_delay_seconds || 1.0}" step="0.1" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('最大转移次数', `<input type="number" class="form-input" id="form-max_request_transfers" value="${currentConfigData.max_request_transfers || 3}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 后台任务
    const bgTaskContent = `
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;">
            ${buildCompactFormGroup('配置监控(秒)', `<input type="number" class="form-input" id="form-config_monitor_interval" value="${backgroundTasks.config_monitor_interval || 30}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('内存监控(秒)', `<input type="number" class="form-input" id="form-memory_monitor_interval" value="${backgroundTasks.memory_monitor_interval || 60}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('过期清理(秒)', `<input type="number" class="form-input" id="form-stale_cleaner_interval" value="${backgroundTasks.stale_cleaner_interval || 60}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 下载和连接
    const downloadContent = `
        <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px;">
            ${buildCompactFormGroup('并发下载', `<input type="number" class="form-input" id="form-max_concurrent_downloads" value="${currentConfigData.max_concurrent_downloads || 3}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('连接超时(秒)', `<input type="number" class="form-input" id="form-download_timeout_connect" value="${downloadTimeout.connect || 10}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('读取超时(秒)', `<input type="number" class="form-input" id="form-download_timeout_sock_read" value="${downloadTimeout.sock_read || 20}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('总超时(秒)', `<input type="number" class="form-input" id="form-download_timeout_total" value="${downloadTimeout.total || 30}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('最大重试', `<input type="number" class="form-input" id="form-download_timeout_max_retries" value="${downloadTimeout.max_retries || 3}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('图床恢复(秒)', `<input type="number" class="form-input" id="form-filebed_recovery_time_seconds" value="${currentConfigData.filebed_recovery_time_seconds || 300}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 连接池
    const poolContent = `
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;">
            ${buildCompactFormGroup('总限制', `<input type="number" class="form-input" id="form-connection_pool_total_limit" value="${connectionPool.total_limit || 200}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('每主机限制', `<input type="number" class="form-input" id="form-connection_pool_per_host_limit" value="${connectionPool.per_host_limit || 50}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('保持连接(秒)', `<input type="number" class="form-input" id="form-connection_pool_keepalive_timeout" value="${connectionPool.keepalive_timeout || 30}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('DNS缓存(秒)', `<input type="number" class="form-input" id="form-connection_pool_dns_cache_ttl" value="${connectionPool.dns_cache_ttl || 300}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 内存管理
    const memoryContent = `
        <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px;">
            ${buildCompactFormGroup('GC阈值(MB)', `<input type="number" class="form-input" id="form-memory_management_gc_threshold_mb" value="${memoryManagement.gc_threshold_mb || 500}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('图片缓存大小', `<input type="number" class="form-input" id="form-memory_management_image_cache_max_size" value="${memoryManagement.image_cache_max_size || 500}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('图片TTL(秒)', `<input type="number" class="form-input" id="form-memory_management_image_cache_ttl_seconds" value="${memoryManagement.image_cache_ttl_seconds || 3600}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('保留数量', `<input type="number" class="form-input" id="form-memory_management_image_cache_keep_size" value="${memoryManagement.image_cache_keep_size || 200}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    // 缓存配置
    const cacheContent = `
        <div style="display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px;">
            ${buildCompactFormGroup('图床TTL', `<input type="number" class="form-input" id="form-cache_settings_filebed_url_cache_ttl" value="${cacheSettings.filebed_url_cache_ttl || 300}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('图床大小', `<input type="number" class="form-input" id="form-cache_settings_filebed_url_cache_max_size" value="${cacheSettings.filebed_url_cache_max_size || 500}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('图片缓存', `<input type="number" class="form-input" id="form-cache_settings_processed_image_cache_max_size" value="${cacheSettings.processed_image_cache_max_size || 200}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('Tiktoken', `<input type="number" class="form-input" id="form-cache_settings_tiktoken_cache_max_size" value="${cacheSettings.tiktoken_cache_max_size || 10}" style="padding: 4px; font-size: 0.8rem;">`)}
            ${buildCompactFormGroup('URL历史', `<input type="number" class="form-input" id="form-cache_settings_downloaded_urls_max_size" value="${cacheSettings.downloaded_urls_max_size || 5000}" style="padding: 4px; font-size: 0.8rem;">`)}
        </div>
    `;
    
    return buildCollapsibleSection('⏱️ 超时配置', timeoutContent) +
           buildCollapsibleSection('🖥️ 服务器配置', serverContent) +
           buildCollapsibleSection('⚙️ 后台任务', bgTaskContent) +
           buildCollapsibleSection('📥 下载连接', downloadContent) +
           buildCollapsibleSection('🔗 连接池', poolContent) +
           buildCollapsibleSection('💾 内存管理', memoryContent) +
           buildCollapsibleSection('📦 缓存配置', cacheContent);
}

function buildReasoningConfig() {
    const content = `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: start;">
            <div style="display: flex; flex-direction: column; gap: 6px;">
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-enable_lmarena_reasoning" ${currentConfigData.enable_lmarena_reasoning ? 'checked' : ''} style="margin-right: 6px;"><strong>启用 LMArena Reasoning</strong></label>
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-preserve_streaming" ${currentConfigData.preserve_streaming ? 'checked' : ''} style="margin-right: 6px;">保留流式传输</label>
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-strip_reasoning_from_history" ${currentConfigData.strip_reasoning_from_history ? 'checked' : ''} style="margin-right: 6px;">从历史剥离Reasoning</label>
            </div>
            ${buildCompactFormGroup('输出模式', `<select class="form-select" id="form-reasoning_output_mode" style="padding: 6px 8px; font-size: 0.85rem;">
                <option value="openai" ${currentConfigData.reasoning_output_mode === 'openai' ? 'selected' : ''}>OpenAI</option>
                <option value="anthropic" ${currentConfigData.reasoning_output_mode === 'anthropic' ? 'selected' : ''}>Anthropic</option>
            </select>`)}
        </div>
    `;
    return buildCollapsibleSection('🧠 Reasoning 配置', content);
}

function buildOtherConfig() {
    const content = `
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 10px;">
            <div style="display: flex; flex-direction: column; gap: 6px;">
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-debug_show_full_urls" ${currentConfigData.debug_show_full_urls ? 'checked' : ''} style="margin-right: 6px;">调试时显示完整URL</label>
                <label style="font-size: 0.85rem;"><input type="checkbox" id="form-enable_idle_restart" ${currentConfigData.enable_idle_restart ? 'checked' : ''} style="margin-right: 6px;">启用空闲重启</label>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px;">
                ${buildCompactFormGroup('URL显示长度', `<input type="number" class="form-input" id="form-url_display_length" value="${currentConfigData.url_display_length || 200}" style="padding: 4px; font-size: 0.8rem;">`)}
                ${buildCompactFormGroup('空闲重启(秒)', `<input type="number" class="form-input" id="form-idle_restart_timeout_seconds" value="${currentConfigData.idle_restart_timeout_seconds || -1}" style="padding: 4px; font-size: 0.8rem;">`)}
            </div>
        </div>
        <div style="margin-top: 10px;">
            ${buildCompactFormGroup('API Key', `<input type="password" class="form-input" id="form-api_key" value="${currentConfigData.api_key || ''}" placeholder="留空则不启用认证" style="padding: 6px 8px; font-size: 0.85rem;">`, 'Tokenizer 配置请在专门的 Tokenizer 页面配置')}
        </div>
    `;
    return buildCollapsibleSection('🔧 其他设置', content);
}

// ==================== 从表单收集数据 ====================
function formToConfig() {
    // 这个函数需要从所有表单字段收集数据
    // 由于字段太多，这里使用简化版本，保留原配置并更新表单字段
    const config = Object.assign({}, currentConfigData);
    
    // 基础配置
    config.version = document.getElementById('form-version').value;
    config.server_port = parseInt(document.getElementById('form-server_port').value) || 5102;
    config.session_id = document.getElementById('form-session_id').value;
    
    // ID更新器配置
    config.id_updater_last_mode = document.getElementById('form-id_updater_last_mode').value;
    config.id_updater_battle_target = document.getElementById('form-id_updater_battle_target').value;
    config.id_updater_auto_save_mode = document.getElementById('form-id_updater_auto_save_mode').value;
    
    // 重试配置
    config.enable_auto_retry = document.getElementById('form-enable_auto_retry').checked;
    config.retry_timeout_seconds = parseInt(document.getElementById('form-retry_timeout_seconds').value);
    
    config.empty_response_retry = config.empty_response_retry || {};
    config.empty_response_retry.enabled = document.getElementById('form-empty_response_retry_enabled').checked;
    config.empty_response_retry.max_retries = parseInt(document.getElementById('form-empty_response_retry_max_retries').value);
    config.empty_response_retry.base_delay_ms = parseInt(document.getElementById('form-empty_response_retry_base_delay_ms').value);
    config.empty_response_retry.max_delay_ms = parseInt(document.getElementById('form-empty_response_retry_max_delay_ms').value);
    config.empty_response_retry.show_retry_info_to_client = document.getElementById('form-empty_response_retry_show_retry_info_to_client').checked;
    
    // Bypass配置
    config.bypass_enabled = document.getElementById('form-bypass_enabled').checked;
    config.bypass_settings = config.bypass_settings || {};
    config.bypass_settings.text = document.getElementById('form-bypass_settings_text').checked;
    config.bypass_settings.search = document.getElementById('form-bypass_settings_search').checked;
    config.bypass_settings.image = document.getElementById('form-bypass_settings_image').checked;
    
    config.attachment_bypass_settings = config.attachment_bypass_settings || {};
    config.attachment_bypass_settings.text = document.getElementById('form-attachment_bypass_settings_text').checked;
    config.attachment_bypass_settings.search = document.getElementById('form-attachment_bypass_settings_search').checked;
    config.attachment_bypass_settings.image = document.getElementById('form-attachment_bypass_settings_image').checked;
    
    // 图像配置
    config.save_images_locally = document.getElementById('form-save_images_locally').checked;
    
    // 消息转换
    config.message_role_conversion_mode = document.getElementById('form-message_role_conversion_mode').value;
    config.merge_preserve_role_labels = document.getElementById('form-merge_preserve_role_labels').checked;
    config.tavern_mode_enabled = document.getElementById('form-tavern_mode_enabled').checked;
    
    // 图床
    config.file_bed_enabled = document.getElementById('form-file_bed_enabled').checked;
    config.file_bed_selection_strategy = document.getElementById('form-file_bed_selection_strategy').value;
    
    // 超时配置
    config.stream_response_timeout_seconds = parseInt(document.getElementById('form-stream_response_timeout_seconds').value);
    config.api_call_timeout_seconds = parseInt(document.getElementById('form-api_call_timeout_seconds').value);
    config.first_chunk_timeout_seconds = parseInt(document.getElementById('form-first_chunk_timeout_seconds').value);
    config.websocket_send_timeout_seconds = parseFloat(document.getElementById('form-websocket_send_timeout_seconds').value);
    config.metadata_timeout_minutes = parseInt(document.getElementById('form-metadata_timeout_minutes').value);
    config.active_request_timeout_minutes = parseInt(document.getElementById('form-active_request_timeout_minutes').value);
    config.tokenizer_idle_timeout_seconds = parseInt(document.getElementById('form-tokenizer_idle_timeout_seconds').value);
    
    // 服务器配置
    config.use_default_ids_if_mapping_not_found = document.getElementById('form-use_default_ids_if_mapping_not_found').checked;
    config.verification_cooldown_seconds = parseInt(document.getElementById('form-verification_cooldown_seconds').value);
    config.stream_end_wait_delay_seconds = parseFloat(document.getElementById('form-stream_end_wait_delay_seconds').value);
    config.max_request_transfers = parseInt(document.getElementById('form-max_request_transfers').value);
    config.load_balancer_lock_timeout_seconds = parseFloat(document.getElementById('form-load_balancer_lock_timeout_seconds').value);
    
    // 后台任务配置
    config.background_tasks = config.background_tasks || {};
    config.background_tasks.config_monitor_interval = parseInt(document.getElementById('form-config_monitor_interval').value);
    config.background_tasks.memory_monitor_interval = parseInt(document.getElementById('form-memory_monitor_interval').value);
    config.background_tasks.stale_cleaner_interval = parseInt(document.getElementById('form-stale_cleaner_interval').value);
    
    // 下载和连接配置
    config.max_concurrent_downloads = parseInt(document.getElementById('form-max_concurrent_downloads').value);
    config.download_timeout = config.download_timeout || {};
    config.download_timeout.connect = parseInt(document.getElementById('form-download_timeout_connect').value);
    config.download_timeout.sock_read = parseInt(document.getElementById('form-download_timeout_sock_read').value);
    config.download_timeout.total = parseInt(document.getElementById('form-download_timeout_total').value);
    config.download_timeout.max_retries = parseInt(document.getElementById('form-download_timeout_max_retries').value);
    config.filebed_recovery_time_seconds = parseInt(document.getElementById('form-filebed_recovery_time_seconds').value);
    
    // 连接池配置
    config.connection_pool = config.connection_pool || {};
    config.connection_pool.total_limit = parseInt(document.getElementById('form-connection_pool_total_limit').value);
    config.connection_pool.per_host_limit = parseInt(document.getElementById('form-connection_pool_per_host_limit').value);
    config.connection_pool.keepalive_timeout = parseInt(document.getElementById('form-connection_pool_keepalive_timeout').value);
    config.connection_pool.dns_cache_ttl = parseInt(document.getElementById('form-connection_pool_dns_cache_ttl').value);
    
    // 内存管理
    config.memory_management = config.memory_management || {};
    config.memory_management.gc_threshold_mb = parseInt(document.getElementById('form-memory_management_gc_threshold_mb').value);
    config.memory_management.image_cache_max_size = parseInt(document.getElementById('form-memory_management_image_cache_max_size').value);
    config.memory_management.image_cache_ttl_seconds = parseInt(document.getElementById('form-memory_management_image_cache_ttl_seconds').value);
    config.memory_management.image_cache_keep_size = parseInt(document.getElementById('form-memory_management_image_cache_keep_size').value);
    
    // 缓存配置
    config.cache_settings = config.cache_settings || {};
    config.cache_settings.filebed_url_cache_ttl = parseInt(document.getElementById('form-cache_settings_filebed_url_cache_ttl').value);
    config.cache_settings.filebed_url_cache_max_size = parseInt(document.getElementById('form-cache_settings_filebed_url_cache_max_size').value);
    config.cache_settings.processed_image_cache_max_size = parseInt(document.getElementById('form-cache_settings_processed_image_cache_max_size').value);
    config.cache_settings.tiktoken_cache_max_size = parseInt(document.getElementById('form-cache_settings_tiktoken_cache_max_size').value);
    config.cache_settings.downloaded_urls_max_size = parseInt(document.getElementById('form-cache_settings_downloaded_urls_max_size').value);
    
    // Reasoning
    config.enable_lmarena_reasoning = document.getElementById('form-enable_lmarena_reasoning').checked;
    config.reasoning_output_mode = document.getElementById('form-reasoning_output_mode').value;
    config.preserve_streaming = document.getElementById('form-preserve_streaming').checked;
    config.strip_reasoning_from_history = document.getElementById('form-strip_reasoning_from_history').checked;
    
    // 其他
    config.debug_show_full_urls = document.getElementById('form-debug_show_full_urls').checked;
    config.url_display_length = parseInt(document.getElementById('form-url_display_length').value);
    config.enable_idle_restart = document.getElementById('form-enable_idle_restart').checked;
    config.idle_restart_timeout_seconds = parseInt(document.getElementById('form-idle_restart_timeout_seconds').value);
    config.api_key = document.getElementById('form-api_key').value;
    
    return config;
}

// ==================== 加载和保存配置 ====================
async function loadConfig() {
    try {
        const response = await fetch('/api/admin/config');
        
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
        document.getElementById('config-editor').value = data.content;
        
        try {
            currentConfigData = parseJsonc(data.content);
        } catch (e) {
            console.error('解析配置失败:', e);
            showConfigMessage('danger', '解析配置失败，请检查JSON格式');
        }
        
        if (currentConfigData) {
            buildConfigForm();
        }
    } catch (error) {
        console.error('❌ 加载配置失败:', error);
        console.error('错误详情:', error.message);
        showConfigMessage('danger', '加载配置失败: ' + error.message);
    }
}

async function saveConfig() {
    let content;
    
    try {
        if (currentConfigMode === 'jsonc') {
            content = document.getElementById('config-editor').value;
        } else {
            const configObj = formToConfig();
            content = JSON.stringify(configObj, null, 2);
        }
        
        const response = await fetch('/api/admin/config', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ content })
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
        
        showConfigMessage('success', '配置已保存并重新加载');
        await loadConfig();
        
    } catch (error) {
        console.error('❌ 保存配置失败:', error);
        console.error('错误详情:', error.message);
        showConfigMessage('danger', '保存失败: ' + error.message);
    }
}

// ==================== 监控页面初始化 ====================
function initMonitorPage() {
    // iframe会自动加载monitor.html
}