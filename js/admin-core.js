// admin-core.js - 核心功能和工具函数
// ==================== 全局会话失效处理 ====================
// admin 各页几十处裸 fetch 不检查 401，会话过期后只会显示空数据。
// 这里统一包装 window.fetch：遇到 401/403 弹一次横幅引导重新登录，
// 原始 Response 原样返回，各调用方原有错误分支不受影响。
(function() {
    if (window.__adminFetchPatched) return;
    window.__adminFetchPatched = true;
    const _origFetch = window.fetch.bind(window);
    let _sessionInvalid = false;
    window.fetch = async function(...args) {
        const resp = await _origFetch(...args);
        if ((resp.status === 401 || resp.status === 403) && !_sessionInvalid) {
            _sessionInvalid = true;
            if (typeof showMessage === 'function') {
                showMessage('danger', '登录会话已失效，请重新登录');
            }
            const banner = document.createElement('div');
            banner.id = 'admin-session-banner';
            banner.style.cssText = 'background:#7f1d1d;color:#fca5a5;padding:10px 20px;text-align:center;font-size:0.85rem;position:sticky;top:0;z-index:9999;';
            banner.innerHTML = '会话已失效，<a href="/login?next=/admin" style="color:#fff;text-decoration:underline;">点此重新登录</a>';
            const header = document.querySelector('.container') || document.body;
            header.insertAdjacentElement('beforebegin', banner);
        }
        return resp;
    };
})();

// ==================== 全局变量 ====================
let currentEditingModel = null;
let currentEditingArchived = false;
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

// HTML转义（防止错误消息/服务端回显内容注入HTML）
function escapeHtml(unsafe) {
    if (unsafe === null || unsafe === undefined) return '';
    return String(unsafe)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}

// HTML 属性值转义。
// 放在 core 里：这个函数被 admin-apikeys.js / admin-models-edit.js 共用，
// 之前只定义在 admin-models-edit.js 中，能跑通纯粹靠 <script> 的加载顺序。
function escapeHtmlForAttr(str) {
    if (str === null || str === undefined) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function showMessage(type, message) {
    _messageCount++;
    const offset = (_messageCount - 1) * 70; // 每条消息偏移 70px
    const notification = document.createElement('div');
    notification.className = `alert alert-${type}`;
    notification.style.cssText = `position: fixed; top: ${20 + offset}px; right: 20px; z-index: 2000; min-width: 300px;`;
    notification.innerHTML = `
        <span>${type === 'success' ? '✅' : '❌'}</span>
        <div>${escapeHtml(message)}</div>
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
            <div>${escapeHtml(message)}</div>
        </div>
    `;
    setTimeout(() => msgEl.innerHTML = '', 5000);
}

// ==================== JSONC 解析器 ====================
// parseJsonc / updateJsoncValues are provided by jsonc-utils.js.

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
            if (memoryDetailsEl) memoryDetailsEl.innerHTML = `<span style="color: #f87171;">${escapeHtml(data.error)}</span>`;
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
                        html += `<div>• ${escapeHtml(t.name)}: ${idleMinutes.toFixed(1)}分钟</div>`;
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
