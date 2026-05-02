// admin-core.js - 核心功能和工具函数

// ==================== 全局变量 ====================
let currentEditingModel = null;
let currentEditingTokenizer = null;
let selectedCaptureMode = 'direct_chat';
let selectedBattleTarget = 'A';
let currentConfigMode = 'form';
let currentConfigData = null;

// 日期过滤
let currentStartDate = null;
let currentEndDate = null;
let currentRequestStartDate = null;
let currentRequestEndDate = null;

// 捕获状态轮询
let capturePollingInterval = null;

// 拖动排序实例
let modelsSortable = null;

// ==================== 页面切换 ====================
function initNavigation() {
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            const page = item.dataset.page;
            
            document.querySelectorAll('.nav-item').forEach(i => i.classList.remove('active'));
            item.classList.add('active');
            
            document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
            document.getElementById(page).classList.add('active');
            
            loadPageData(page);
        });
    });
}

function loadPageData(page) {
    // 🔧 修复：切离 monitor 页时隐藏 iframe 而非卸载（避免 fetch 被取消导致 TypeError: Failed to fetch）
    const monitorIframe = document.getElementById('monitor-iframe');
    const monitorPage = document.getElementById('monitor');
    
    if (page === 'monitor') {
        // 显示监控页面容器（清除内联样式，让 CSS .page.active 控制显示）
        if (monitorPage) monitorPage.style.display = '';
        // 首次加载时才设置 src
        if (monitorIframe && !monitorIframe.getAttribute('data-loaded')) {
            monitorIframe.src = '/monitor';
            monitorIframe.setAttribute('data-loaded', '1');
        }
    } else {
        // 隐藏监控页面（保留 iframe 内容，避免 fetch 请求被取消）
        if (monitorPage) monitorPage.style.display = 'none';
    }

    switch(page) {
        case 'overview':
            refreshOverview();
            break;
        case 'models':
            loadModels();
            break;
        case 'tokenizer':
            refreshTokenizerInfo();
            loadTokenizerMappings();
            loadCustomTokenizers();
            break;
        case 'config':
            loadConfig();
            break;
        case 'token-calculator':
            // Token计算器使用iframe加载，不需要额外初始化
            break;
        case 'api-keys':
            if (typeof loadApiKeys === 'function') loadApiKeys();
            break;
        case 'monitor':
            // iframe 已在上面处理
            break;
    }
}

// ==================== 工具函数 ====================
function formatNumber(num) {
    if (num >= 1000000) {
        return (num / 1000000).toFixed(1) + 'M';
    } else if (num >= 1000) {
        return (num / 1000).toFixed(1) + 'K';
    }
    return num.toString();
}

function formatMonitorDuration(seconds) {
    if (!seconds || seconds < 0) return '0s';
    if (seconds < 1) return (seconds * 1000).toFixed(0) + 'ms';
    if (seconds < 60) return seconds.toFixed(1) + 's';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    return Math.floor(seconds / 3600) + 'h';
}

function generateColors(count) {
    const colors = [
        'rgba(42, 168, 255, 0.8)',
        'rgba(16, 185, 129, 0.8)',
        'rgba(245, 158, 11, 0.8)',
        'rgba(239, 68, 68, 0.8)',
        'rgba(168, 85, 247, 0.8)',
        'rgba(236, 72, 153, 0.8)',
        'rgba(59, 130, 246, 0.8)',
        'rgba(34, 197, 94, 0.8)',
        'rgba(251, 146, 60, 0.8)',
        'rgba(244, 63, 94, 0.8)'
    ];
    return colors.slice(0, count);
}

// ==================== 消息提示 ====================
let _messageCount = 0; // 消息计数器，避免重叠

function showMessage(type, message) {
    _messageCount++;
    const offset = (_messageCount - 1) * 70; // 每条消息偏移 70px
    const notification = document.createElement('div');
    notification.className = `alert alert-${type}`;
    notification.style.cssText = `position: fixed; top: ${20 + offset}px; right: 20px; z-index: 2000; min-width: 300px;`;
    notification.innerHTML = `
        <span>${type === 'success' ? '✅' : '❌'}</span>
        <div>${message}</div>
    `;
    document.body.appendChild(notification);
    setTimeout(() => { notification.remove(); _messageCount = Math.max(0, _messageCount - 1); }, 5000);
}

function showQuietMessage(type, message) {
    const notification = document.createElement('div');
    notification.style.cssText = `
        position: fixed;
        bottom: 20px;
        right: 20px;
        z-index: 2000;
        padding: 10px 20px;
        background: ${type === 'success' ? 'rgba(16, 185, 129, 0.9)' : 'rgba(239, 68, 68, 0.9)'};
        color: white;
        border-radius: 6px;
        font-size: 0.875rem;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        opacity: 0;
        transition: opacity 0.3s;
    `;
    notification.textContent = message;
    document.body.appendChild(notification);
    
    setTimeout(() => notification.style.opacity = '1', 10);
    setTimeout(() => {
        notification.style.opacity = '0';
        setTimeout(() => notification.remove(), 300);
    }, 3000);
}

function showConfigMessage(type, message) {
    const msgEl = document.getElementById('config-message');
    msgEl.innerHTML = `
        <div class="alert alert-${type}">
            <span>${type === 'success' ? '✅' : '❌'}</span>
            <div>${message}</div>
        </div>
    `;
    setTimeout(() => msgEl.innerHTML = '', 5000);
}

// ==================== JSONC 解析器 ====================
function parseJsonc(content) {
    let result = '';
    let i = 0;
    let inString = false;
    let stringChar = '';
    
    while (i < content.length) {
        const char = content[i];
        const nextChar = content[i + 1];
        
        if (!inString && (char === '"' || char === "'")) {
            inString = true;
            stringChar = char;
            result += char;
            i++;
            continue;
        } else if (inString && char === stringChar && content[i - 1] !== '\\') {
            inString = false;
            result += char;
            i++;
            continue;
        } else if (inString) {
            result += char;
            i++;
            continue;
        }
        
        if (char === '/' && nextChar === '/') {
            while (i < content.length && content[i] !== '\n') {
                i++;
            }
            continue;
        }
        
        if (char === '/' && nextChar === '*') {
            i += 2;
            while (i < content.length - 1) {
                if (content[i] === '*' && content[i + 1] === '/') {
                    i += 2;
                    break;
                }
                i++;
            }
            continue;
        }
        
        result += char;
        i++;
    }
    
    result = result.replace(/,(\s*[}\]])/g, '$1');
    return JSON.parse(result);
}

// ==================== 内存监控 ====================
async function refreshMemoryInfo() {
    try {
        const response = await fetch('/api/monitor/memory');
        const data = await response.json();
        
        const memoryValueEl = document.getElementById('memory-value');
        const memoryDetailsEl = document.getElementById('memory-details');
        const memoryBarFillEl = document.getElementById('memory-bar-fill');
        const tokenizerInfoEl = document.getElementById('tokenizer-info');
        const clearCacheBtn = document.getElementById('clear-cache-btn');
        
        if (data.error) {
            console.error('获取内存信息错误:', data.error);
            if (memoryValueEl) memoryValueEl.textContent = '错误';
            if (memoryDetailsEl) memoryDetailsEl.innerHTML = `<span style="color: #f87171;">${data.error}</span>`;
            return;
        }
        
        // 更新进程内存
        const processMemoryMb = data.process?.rss_mb || 0;
        const systemPercent = data.system?.percent || 0;
        
        if (memoryValueEl) {
            memoryValueEl.textContent = `${processMemoryMb.toFixed(1)} MB`;
        }
        
        // 更新进度条
        if (memoryBarFillEl) {
            // 假设最大显示1GB
            const percent = Math.min((processMemoryMb / 1024) * 100, 100);
            memoryBarFillEl.style.width = `${percent}%`;
            // 根据内存使用量改变颜色
            if (processMemoryMb > 500) {
                memoryBarFillEl.style.background = '#ef4444'; // 红色
            } else if (processMemoryMb > 300) {
                memoryBarFillEl.style.background = '#f59e0b'; // 橙色
            } else {
                memoryBarFillEl.style.background = 'var(--accent)'; // 蓝色
            }
        }
        
        // 更新分词器缓存信息
        const tokenizerData = data.tokenizers;
        if (tokenizerData && !tokenizerData.error) {
            const loadedCount = tokenizerData.loaded_count || 0;
            const estimatedMb = tokenizerData.estimated_memory_mb || 0;
            const loadedTokenizers = tokenizerData.loaded_tokenizers || [];
            
            // 更新详情文本
            if (memoryDetailsEl) {
                if (loadedCount > 0) {
                    memoryDetailsEl.innerHTML = `Tokenizer: ${loadedCount}个 (~${estimatedMb.toFixed(0)}MB)`;
                } else {
                    memoryDetailsEl.textContent = '无Tokenizer缓存';
                }
            }
            
            // 更新分词器详情列表
            if (tokenizerInfoEl) {
                if (loadedTokenizers.length > 0) {
                    let html = '';
                    for (const t of loadedTokenizers) {
                        const idleMinutes = t.idle_minutes || 0;
                        html += `<div>• ${t.name}: ${idleMinutes.toFixed(1)}分钟</div>`;
                    }
                    tokenizerInfoEl.innerHTML = html;
                    tokenizerInfoEl.style.display = 'block';
                } else {
                    tokenizerInfoEl.style.display = 'none';
                }
            }
            
            // 显示/隐藏清理按钮
            if (clearCacheBtn) {
                clearCacheBtn.style.display = loadedCount > 0 ? 'block' : 'none';
            }
        } else {
            if (memoryDetailsEl) {
                memoryDetailsEl.textContent = tokenizerData?.error || 'Tokenizer信息不可用';
            }
            if (tokenizerInfoEl) tokenizerInfoEl.style.display = 'none';
            if (clearCacheBtn) clearCacheBtn.style.display = 'none';
        }
    } catch (error) {
        console.error('刷新内存信息失败:', error);
        const memoryValueEl = document.getElementById('memory-value');
        const memoryDetailsEl = document.getElementById('memory-details');
        if (memoryValueEl) memoryValueEl.textContent = '错误';
        if (memoryDetailsEl) memoryDetailsEl.innerHTML = '<span style="color: #f87171;">请求失败</span>';
    }
}

async function clearTokenizerCache() {
    try {
        const response = await fetch('/api/monitor/clear_tokenizer_cache', {
            method: 'POST'
        });
        const data = await response.json();
        
        if (data.error) {
            showQuietMessage('error', data.error);
        } else {
            const clearedCount = data.cleared_count || 0;
            const freedMb = data.memory_freed_mb || 0;
            showQuietMessage('success', `已清理 ${clearedCount} 个分词器，释放 ${freedMb.toFixed(1)} MB`);
            refreshMemoryInfo();
        }
    } catch (error) {
        showQuietMessage('error', '清理缓存请求失败');
        console.error('清理分词器缓存失败:', error);
    }
}

// ==================== 初始化 ====================
document.addEventListener('DOMContentLoaded', () => {
    initNavigation();

    // 🔧 优化：后端查询已异步化（asyncio.to_thread），所有请求可以并行发起
    refreshOverview();
    refreshMemoryInfo();
    refreshRequestStats();
    
    // 🔧 B7 修复：使用 visibility API，页面不可见时暂停所有定时器
    let _overviewTimer = null;
    let _memoryTimer = null;

    function _startTimers() {
        if (!_overviewTimer) {
            _overviewTimer = setInterval(() => {
                if (document.getElementById('overview').classList.contains('active')) {
                    refreshOverview();
                }
            }, 300000); // 300秒刷新一次（从120秒降频）
        }
        if (!_memoryTimer) {
            _memoryTimer = setInterval(refreshMemoryInfo, 30000); // 30秒（从10秒降频）
        }
    }

    function _stopTimers() {
        if (_overviewTimer) { clearInterval(_overviewTimer); _overviewTimer = null; }
        if (_memoryTimer) { clearInterval(_memoryTimer); _memoryTimer = null; }
    }

    _startTimers();

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            _stopTimers();
        } else {
            _startTimers();
            // 恢复可见时立即刷新一次
            refreshMemoryInfo();
        }
    });
});