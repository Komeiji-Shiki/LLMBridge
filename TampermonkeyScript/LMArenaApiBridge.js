// ==UserScript==
// @name         LMArena API Bridge
// @namespace    http://tampermonkey.net/
// @version      2.9
// @description  Bridges LMArena to a local API server via WebSocket. Uses new post-to-evaluation API endpoint.
// @author       Lianues
// @match        https://lmarena.ai/*
// @match        https://*.lmarena.ai/*
// @icon         https://www.google.com/s2/favicons?sz=64&domain=lmarena.ai
// @grant        none
// @run-at       document-end
// ==/UserScript==

(function () {
    'use strict';

    // --- 配置 ---
    const SERVER_URL = "ws://localhost:5102/ws"; // 与 api_server.py 中的端口匹配
    let socket;
    let isCaptureModeActive = false; // ID捕获模式的开关

    // --- 生成唯一的标签页ID ---
    const TAB_ID = `tab_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
    console.log(`[API Bridge] 本标签页ID: ${TAB_ID}`);
    
    // 🔧 终极修复：追踪活动请求及其AbortController和取消状态
    const activeRequests = new Map(); // Map<requestId, { controller: AbortController, cancelled: boolean }>

    // --- 页面可见性管理 ---
    const visibilityManager = {
        isHidden: document.hidden,
        bufferQueue: [],
        bufferTimer: null,

        init() {
            document.addEventListener('visibilitychange', () => {
                this.isHidden = document.hidden;
                // 页面可见性变化日志已移除（减少控制台噪音）

                // 当页面变为可见时，立即发送缓冲的数据
                if (!this.isHidden && this.bufferQueue.length > 0) {
                    this.flushBuffer();
                }
            });
        },

        flushBuffer() {
            if (this.bufferQueue.length === 0) return;

            const combinedData = this.bufferQueue.join('');
            this.bufferQueue = [];

            if (this.bufferTimer) {
                clearTimeout(this.bufferTimer);
                this.bufferTimer = null;
            }

            // 直接发送组合的数据
            return combinedData;
        },

        scheduleFlush(requestId, sendFn, delay = 100) {
            if (this.bufferTimer) {
                clearTimeout(this.bufferTimer);
            }

            this.bufferTimer = setTimeout(() => {
                const data = this.flushBuffer();
                if (data) {
                    sendFn(requestId, data);
                }
                this.bufferTimer = null;
            }, delay);
        }
    };

    // --- 初始化页面可见性管理 ---
    visibilityManager.init();

    // --- 核心逻辑 ---
    function connect() {
        console.log(`[API Bridge] 正在连接到本地服务器: ${SERVER_URL}...`);
        socket = new WebSocket(SERVER_URL);

        socket.onopen = () => {
            console.log("[API Bridge] ✅ 与本地服务器的 WebSocket 连接已建立。");
            console.log(`[API Bridge] 发送标签页ID: ${TAB_ID}`);

            // 立即发送标签页ID给服务器
            socket.send(JSON.stringify({ tab_id: TAB_ID }));

            document.title = "✅ " + document.title;
        };

        socket.onmessage = async (event) => {
            try {
                const message = JSON.parse(event.data);

                // 检查是否是指令，而不是标准的聊天请求
                if (message.command) {
                    console.log(`[API Bridge] ⬇️ 收到指令: ${message.command}`);
                    if (message.command === 'refresh' || message.command === 'reconnect') {
                        console.log(`[API Bridge] 收到 '${message.command}' 指令，正在执行页面刷新...`);
                        location.reload();
                    } else if (message.command === 'activate_id_capture') {
                        console.log("[API Bridge] ✅ ID 捕获模式已激活。请在页面上触发一次 'Retry' 操作。");
                        isCaptureModeActive = true;
                        // 可以选择性地给用户一个视觉提示
                        document.title = "🎯 " + document.title;
                    } else if (message.command === 'send_page_source') {
                       console.log("[API Bridge] 收到发送页面源码的指令，正在发送...");
                       sendPageSource();
                    } else if (message.command === 'cancel_request' && message.request_id) {
                       // 🔧 核心修复：处理服务器发送的取消指令
                       console.log(`[REQUEST_LIFECYCLE] ❗️ 收到服务器取消指令 for request: ${message.request_id.substring(0, 8)}`);
                       const requestInfo = activeRequests.get(message.request_id);
                       if (requestInfo) {
                           // 标记为已取消（防止继续处理响应）
                           requestInfo.cancelled = true;
                           // 中止fetch请求
                           requestInfo.controller.abort('Cancelled by server due to client disconnect.');
                           console.log(`[REQUEST_LIFECYCLE] ✅ 已标记取消并调用abort(): ${message.request_id.substring(0, 8)}`);
                           console.log(`[REQUEST_LIFECYCLE]   - 该请求的所有后续响应都将被忽略`);
                           // 注意：不在这里delete，让finally块处理清理
                       } else {
                           console.warn(`[REQUEST_LIFECYCLE] ⚠️ 想取消请求但未在activeRequests中找到: ${message.request_id.substring(0, 8)}`);
                       }
                    }
                    return;
                }

                const { request_id, payload } = message;

                if (!request_id || !payload) {
                    console.error("[API Bridge] 收到来自服务器的无效消息:", message);
                    return;
                }

                console.log(`[API Bridge] ⬇️ 收到聊天请求 ${request_id.substring(0, 8)}。准备执行 fetch 操作。`);
                // 传递重试配置
                const retryConfig = message.retry_config || {};
                await executeFetchAndStreamBack(request_id, payload, retryConfig);

            } catch (error) {
                console.error("[API Bridge] 处理服务器消息时出错:", error);
            }
        };

        socket.onclose = () => {
            console.warn("[API Bridge] 🔌 与本地服务器的连接已断开。将在5秒后尝试重新连接...");
            if (document.title.startsWith("✅ ")) {
                document.title = document.title.substring(2);
            }
            setTimeout(connect, 5000);
        };

        socket.onerror = (error) => {
            console.error("[API Bridge] ❌ WebSocket 发生错误:", error);
            socket.close(); // 会触发 onclose 中的重连逻辑
        };
    }

    // UUID v7 Generator - Time-ordered UUID (从 main 版本复制)
    function generateUUIDv7() {
        // Get current timestamp in milliseconds
        const timestamp = Date.now();

        // Generate random bytes for the rest of the UUID
        const randomBytes = new Uint8Array(10);
        crypto.getRandomValues(randomBytes);

        // Convert timestamp to hex (48 bits / 6 bytes)
        const timestampHex = timestamp.toString(16).padStart(12, '0');

        // Build UUID v7 format: xxxxxxxx-xxxx-7xxx-yxxx-xxxxxxxxxxxx
        // where x is timestamp or random, 7 is version, y is variant (8, 9, a, or b)

        // First 8 hex chars (32 bits) from timestamp
        const part1 = timestampHex.substring(0, 8);

        // Next 4 hex chars (16 bits) from timestamp
        const part2 = timestampHex.substring(8, 12);

        // Version (4 bits = 7) + 12 bits random
        const part3 = '7' + Array.from(randomBytes.slice(0, 2))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('')
            .substring(1, 4);

        // Variant (2 bits = 10b) + 14 bits random
        const variant = (randomBytes[2] & 0x3f) | 0x80; // Set variant bits to 10xxxxxx
        const part4 = variant.toString(16).padStart(2, '0') +
            randomBytes[3].toString(16).padStart(2, '0');

        // Last 48 bits (12 hex chars) random
        const part5 = Array.from(randomBytes.slice(4, 10))
            .map(b => b.toString(16).padStart(2, '0'))
            .join('');

        return `${part1}-${part2}-${part3}-${part4}-${part5}`;
    }

    async function executeFetchAndStreamBack(requestId, payload, retryConfig, retryCount = 0) {
        console.log(`[API Bridge] 当前操作域名: ${window.location.hostname}`);
        const { is_image_request, message_templates, target_model_id, session_id, battle_target, mode } = payload;

        // 🔍 诊断日志：记录请求生命周期
        console.log(`[REQUEST_LIFECYCLE] 🚀 开始执行请求: ${requestId.substring(0, 8)}`);

        // 🔧 核心修复：在重试开始前检查是否已被取消
        const existingRequestInfo = activeRequests.get(requestId);
        if (existingRequestInfo && existingRequestInfo.cancelled) {
            console.log(`[RETRY_CANCEL] 🛑 请求已被取消，中止重试: ${requestId.substring(0, 8)} (重试次数: ${retryCount})`);
            return; // 直接返回，不执行任何操作
        }

        // 从服务器接收的重试配置（带默认值）
        const retrySettings = retryConfig || {};
        const RETRY_ENABLED = retrySettings.enabled !== false; // 默认启用
        const MAX_RETRIES = retrySettings.max_retries || 5;
        const BASE_DELAY = retrySettings.base_delay_ms || 1000;
        const MAX_DELAY = retrySettings.max_delay_ms || 30000;
        const SHOW_RETRY_INFO = retrySettings.show_retry_info || false;
        
        if (retryCount === 0) {
            console.log(`[RETRY_CONFIG] 使用重试配置: enabled=${RETRY_ENABLED}, max=${MAX_RETRIES}, base_delay=${BASE_DELAY}ms, max_delay=${MAX_DELAY}ms`);
        }

        if (retryCount > 0) {
            console.log(`[API Bridge] 🔄 重试请求 ${requestId.substring(0, 8)}，重试次数: ${retryCount}/${MAX_RETRIES}`);
        }

        // 🔍 诊断日志：追踪WebSocket连接状态
        const wsState = socket ? socket.readyState : 'NO_SOCKET';
        console.log(`[REQUEST_LIFECYCLE] WebSocket状态: ${wsState} (0=CONNECTING, 1=OPEN, 2=CLOSING, 3=CLOSED)`);

        // 关键修复：为每个请求创建独立的buffer，避免并发时内容混串
        const requestBuffer = {
            queue: [],
            timer: null
        };

        // --- 使用从后端配置传递的会话信息 ---
        if (!session_id) {
            const errorMsg = "从后端收到的会话信息 (session_id) 为空。请先运行 `id_updater.py` 脚本进行设置。";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // 新的 URL 格式
        const apiUrl = `/nextjs-api/stream/post-to-evaluation/${session_id}`;
        const httpMethod = 'POST';
        
        // 确定实际使用的模式
        const actualMode = mode || 'battle';
        console.log(`[API Bridge] 使用 API 端点: ${apiUrl}`);
        console.log(`[API Bridge] 模式: ${actualMode}`);
        console.log(`[API Bridge] 目标位置: ${battle_target || 'a'}`);

        if (!message_templates || message_templates.length === 0) {
            const errorMsg = "从后端收到的消息列表为空。";
            console.error(`[API Bridge] ${errorMsg}`);
            sendToServer(requestId, { error: errorMsg });
            sendToServer(requestId, "[DONE]");
            return;
        }

        // 构造新的请求体结构 (需要生成新的 IDs)
        const userMessageId = generateUUIDv7();
        const modelAMessageId = generateUUIDv7();
        const modelBMessageId = generateUUIDv7();
        
        const newMessages = [];
        for (let i = 0; i < message_templates.length; i++) {
            const template = message_templates[i];
            const messageId = generateUUIDv7();
            
            // 构造消息体 (简化版，无需 status, parentMessageIds)
            newMessages.push({
                id: messageId,
                evaluationSessionId: session_id,
                role: template.role,
                parentMessageIds: [],
                content: template.content,
                // LMArena 新 API 使用 experimental_attachments 字段
                experimental_attachments: Array.isArray(template.attachments) ? template.attachments : [],
                participantPosition: template.participantPosition || "a",
            });
        }

        // 根据模式构建不同的请求体
        let body;
        if (actualMode === 'direct_chat' || actualMode === 'direct') {
            // DirectChat 模式：使用 direct 模式，只需要 modelAId
            body = {
                id: session_id,
                mode: "direct",
                modelAId: target_model_id,
                userMessageId: userMessageId,
                modelAMessageId: modelAMessageId,
                messages: newMessages,
                modality: "chat"
            };
            console.log(`[API Bridge] DirectChat 模式，使用 modelAId: ${target_model_id}`);
        } else {
            // Battle 模式
            body = {
                id: session_id,
                mode: "battle",
                userMessageId: userMessageId,
                modelAMessageId: modelAMessageId,
                modelBMessageId: modelBMessageId,
                messages: newMessages,
                modality: "chat"
            };
            console.log(`[API Bridge] Battle 模式`);
        }
        
        console.log("[API Bridge] 准备发送到 LMArena API 的最终载荷:", JSON.stringify(body, null, 2));

        // 🔧 终极修复：为每个请求创建独立的AbortController并追踪取消状态
        // 关键修复：重试时不创建新的 AbortController，而是复用现有的（如果存在）
        let abortController;
        let requestInfo = activeRequests.get(requestId);
        
        if (!requestInfo || retryCount === 0) {
            // 首次请求或不存在时，创建新的
            abortController = new AbortController();
            requestInfo = {
                controller: abortController,
                cancelled: false
            };
            activeRequests.set(requestId, requestInfo);
            console.log(`[REQUEST_LIFECYCLE] 创建新的 AbortController for ${requestId.substring(0, 8)}`);
        } else {
            // 重试时复用现有的 AbortController
            abortController = requestInfo.controller;
            console.log(`[RETRY_CANCEL] 重试时复用现有 AbortController for ${requestId.substring(0, 8)} (已取消: ${requestInfo.cancelled})`);
            
            // 再次检查取消状态（双重保险）
            if (requestInfo.cancelled) {
                console.log(`[RETRY_CANCEL] 🛑 在复用检查时发现已取消，中止重试: ${requestId.substring(0, 8)}`);
                return;
            }
        }
        
        // 设置一个标志，让我们的 fetch 拦截器知道这个请求是脚本自己发起的
        window.isApiBridgeRequest = true;
        try {
            console.log(`[REQUEST_LIFECYCLE] 📡 向LMArena发送fetch请求: ${requestId.substring(0, 8)}`);
            
            const response = await fetch(apiUrl, {
                method: httpMethod,
                headers: {
                    'Content-Type': 'text/plain;charset=UTF-8', // LMArena 使用 text/plain
                    'Accept': '*/*',
                },
                body: JSON.stringify(body),
                credentials: 'include', // 必须包含 cookie
                signal: abortController.signal // 使用与请求关联的signal
            });
            if (!response.ok || !response.body) {
                const errorBody = await response.text();
                throw new Error(`网络响应不正常。状态: ${response.status}. 内容: ${errorBody}`);
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let chunkCount = 0;
            let totalBytes = 0;
            let hasReceivedContent = false; // 标记是否收到实际内容
            let contentChunkCount = 0; // 实际内容块数量
            let emptyResponseDetected = false; // 标记是否检测到空响应
            const startTime = Date.now();
            
            // 改进的空回检测：检测多种模式
            const contentPatterns = [
                /[ab]0:"((?:\\.|[^"\\])*)"/,  // 文本内容
                /ag:"((?:\\.|[^"\\])*)"/,      // 思维链内容
                /[ab]2:(\[.*?\])/,             // 图片内容
                /[ab]d:(\{.*?"finishReason".*?\})/ // 结束信号
            ];

            // 优化的流处理函数 - 使用请求级别的buffer
            const processAndSend = (requestId, data) => {
                if (visibilityManager.isHidden) {
                    // 页面在后台时，批量缓冲数据到请求专属buffer
                    requestBuffer.queue.push(data);

                    // 清除旧timer并设置新的（后台时延迟50ms批处理）
                    if (requestBuffer.timer) {
                        clearTimeout(requestBuffer.timer);
                    }
                    requestBuffer.timer = setTimeout(() => {
                        if (requestBuffer.queue.length > 0) {
                            const combinedData = requestBuffer.queue.join('');
                            requestBuffer.queue = [];
                            sendToServer(requestId, combinedData);
                        }
                        requestBuffer.timer = null;
                    }, 50);
                } else {
                    // 🔧 优化：页面在前台时，完全移除批处理延迟
                    // 先清理任何可能的timer（从后台切换到前台时）
                    if (requestBuffer.timer) {
                        clearTimeout(requestBuffer.timer);
                        requestBuffer.timer = null;
                    }
                    
                    // 立即发送buffer中的数据（如果有）
                    if (requestBuffer.queue.length > 0) {
                        const bufferedData = requestBuffer.queue.join('');
                        requestBuffer.queue = [];
                        sendToServer(requestId, bufferedData);
                    }
                    
                    // 立即发送当前数据（零延迟）
                    sendToServer(requestId, data);
                }
            };

            while (true) {
                // 🔧 关键修复：在每次循环开始时检查是否已被取消
                const currentRequestInfo = activeRequests.get(requestId);
                if (currentRequestInfo && currentRequestInfo.cancelled) {
                    console.log(`[REQUEST_LIFECYCLE] 🛑 检测到请求已被取消，停止读取响应: ${requestId.substring(0, 8)}`);
                    reader.cancel('Request cancelled by server');
                    break;
                }
                
                const { value, done } = await reader.read();
                if (done) {
                    // 🔧 智能空回检测：综合判断
                    const elapsedTime = Date.now() - startTime;
                    const avgChunkSize = chunkCount > 0 ? totalBytes / chunkCount : 0;
                    
                    // 检测条件：
                    // 1. 没有收到任何有意义的内容块
                    // 2. 总字节数太少（小于30字节通常是空响应）
                    // 3. 平均块大小异常小
                    // 4. 响应时间异常短（小于500ms可能是立即失败）
                    const isEmptyResponse = (
                        !hasReceivedContent ||
                        contentChunkCount === 0 ||
                        totalBytes < 30 ||
                        (avgChunkSize < 10 && chunkCount > 0) ||
                        (elapsedTime < 500 && totalBytes < 100)
                    );
                    
                    if (isEmptyResponse) {
                        console.warn(`[EMPTY_DETECTION] ⚠️ 检测到空响应！`);
                        console.warn(`  - 实际内容块: ${contentChunkCount}`);
                        console.warn(`  - 总字节数: ${totalBytes}`);
                        console.warn(`  - 总块数: ${chunkCount}`);
                        console.warn(`  - 平均块大小: ${avgChunkSize.toFixed(2)} bytes`);
                        console.warn(`  - 响应时长: ${elapsedTime}ms`);
                    }
                    // 🔧 诊断日志：检查buffer状态
                    console.log(`[BUFFER_DEBUG] 🛑 请求 ${requestId.substring(0, 8)} 流结束时的buffer状态:`);
                    console.log(`  - requestBuffer.queue长度: ${requestBuffer.queue.length}`);
                    console.log(`  - requestBuffer.queue内容: ${JSON.stringify(requestBuffer.queue)}`);
                    console.log(`  - 未处理的buffer: "${buffer}"`);
                    console.log(`  - buffer长度: ${buffer.length}`);
                    
                    // 🔧 关键修复：在发送[DONE]前，先刷新所有缓冲区
                    // 1. 先发送requestBuffer中的数据
                    if (requestBuffer.queue.length > 0) {
                        const bufferedData = requestBuffer.queue.join('');
                        console.log(`[BUFFER_DEBUG] ⚠️ 发现未发送的缓冲数据！长度: ${bufferedData.length}`);
                        console.log(`[BUFFER_DEBUG] 缓冲内容预览: ${bufferedData.substring(0, 200)}...`);
                        sendToServer(requestId, bufferedData);
                        requestBuffer.queue = [];
                        if (requestBuffer.timer) {
                            clearTimeout(requestBuffer.timer);
                            requestBuffer.timer = null;
                        }
                    }
                    
                    // 2. 发送剩余的buffer（如果有）
                    if (buffer.length > 0) {
                        console.log(`[BUFFER_DEBUG] ⚠️ 发现未处理的buffer！长度: ${buffer.length}`);
                        console.log(`[BUFFER_DEBUG] buffer内容: ${buffer}`);
                        sendToServer(requestId, buffer);
                        buffer = '';
                    }
                    
                    // 🔧 核心修复：等待WebSocket缓冲区清空
                    // 这是关键修复！确保所有数据都已发送完毕
                    let waitCount = 0;
                    const maxWait = 50; // 最多等待5秒（50 * 100ms）
                    while (socket && socket.bufferedAmount > 0 && waitCount < maxWait) {
                        console.log(`[BUFFER_DEBUG] ⏳ WebSocket缓冲区还有 ${socket.bufferedAmount} 字节，等待清空...`);
                        await new Promise(resolve => setTimeout(resolve, 100));
                        waitCount++;
                    }
                    
                    if (waitCount >= maxWait) {
                        console.warn(`[BUFFER_DEBUG] ⚠️ 等待超时，但仍有 ${socket.bufferedAmount} 字节未发送`);
                    }
                    
                    // 额外等待500ms确保服务器端接收完毕
                    console.log(`[BUFFER_DEBUG] ⏳ 额外等待500ms确保服务器端接收完毕...`);
                    await new Promise(resolve => setTimeout(resolve, 500));
                    console.log(`[BUFFER_DEBUG] ✅ 延迟完成，现在发送[DONE]信号`);
                    
                    // 检测空响应并重试（如果启用）
                    if (isEmptyResponse && RETRY_ENABLED) {
                        emptyResponseDetected = true;

                        // 如果还有重试机会
                        if (retryCount < MAX_RETRIES) {
                            const delay = Math.min(BASE_DELAY * Math.pow(2, retryCount), MAX_DELAY);
                            console.log(`[RETRY] ⏳ 等待 ${delay/1000} 秒后重试 (${retryCount + 1}/${MAX_RETRIES})...`);

                            // 只在配置允许时向客户端显示重试信息
                            if (SHOW_RETRY_INFO) {
                                sendToServer(requestId, {
                                    retry_info: {
                                        attempt: retryCount + 1,
                                        max_attempts: MAX_RETRIES,
                                        delay: delay,
                                        reason: "Empty response detected (smart detection)"
                                    }
                                });
                            }

                            // 等待期间允许请求被中止
                            try {
                                await new Promise((resolve, reject) => {
                                    const timeoutId = setTimeout(resolve, delay);
                                    abortController.signal.addEventListener('abort', () => {
                                        clearTimeout(timeoutId);
                                        reject(new DOMException('Retry delay aborted', 'AbortError'));
                                    });
                                });
                            } catch (abortError) {
                                if (abortError.name === 'AbortError') {
                                    console.log(`[RETRY_CANCEL] 🛑 重试等待期间被取消: ${requestId.substring(0, 8)}`);
                                    return; // 中止重试
                                }
                                throw abortError;
                            }
                            
                            // 🔧 核心修复：在执行重试前再次检查取消状态
                            const currentRequestInfo = activeRequests.get(requestId);
                            if (currentRequestInfo && currentRequestInfo.cancelled) {
                                console.log(`[RETRY_CANCEL] 🛑 重试前检测到取消，中止重试: ${requestId.substring(0, 8)}`);
                                return;
                            }
                            
                            await executeFetchAndStreamBack(requestId, payload, retryConfig, retryCount + 1);
                            return;
                        } else {
                            throw new Error(`Empty response after ${MAX_RETRIES} retries (smart detection).`);
                        }
                    } else if (isEmptyResponse && !RETRY_ENABLED) {
                        console.warn(`[RETRY] ⚠️ 检测到空响应但重试已禁用，直接返回`);
                    }

                    // 正常响应结束
                    console.log(`[REQUEST_LIFECYCLE] ✅ 请求 ${requestId.substring(0, 8)} 的流已成功结束（所有buffer已刷新）。`);
                    sendToServer(requestId, "[DONE]");
                    break;
                }

                chunkCount++;
                totalBytes += value.length;

                const chunk = decoder.decode(value, { stream: true });
                
                // 🔧 关键修复：在发送数据前再次检查是否已被取消
                const requestInfoBeforeSend = activeRequests.get(requestId);
                if (requestInfoBeforeSend && requestInfoBeforeSend.cancelled) {
                    console.log(`[REQUEST_LIFECYCLE] 🛑 在发送数据前检测到取消，丢弃此块: ${requestId.substring(0, 8)}`);
                    continue; // 跳过此块，不发送
                }
                
                // 🔧 关键修复：立即发送原始chunk，不做任何预处理
                // 让后端的Python代码来处理正则匹配和内容提取
                // 这样可以避免JS端正则匹配不完整导致的数据丢失
                if (chunk) {
                    // 改进的内容检测：检查是否包含实际内容
                    let hasActualContent = false;
                    for (const pattern of contentPatterns) {
                        if (pattern.test(chunk)) {
                            hasActualContent = true;
                            contentChunkCount++;
                            break;
                        }
                    }
                    
                    if (hasActualContent) {
                        hasReceivedContent = true;
                    }
                    
                    // 核心修改：根据 battle_target 过滤数据块
                    const targetPosition = battle_target || 'a';
                    const filteredChunk = filterStreamByTarget(chunk, targetPosition);
                    
                    if (filteredChunk) {
                        processAndSend(requestId, filteredChunk);
                    }
                }
    
                // 🔧 关键修复：使用背压控制替代RAF限速
                // RAF在前台限制60fps(16ms/chunk)，后台限制1fps(1000ms/chunk)，导致严重堆积
                // 改用WebSocket缓冲区监控实现智能背压
                if (socket && socket.bufferedAmount > 65536) { // 64KB硬阈值
                    console.log(`[BACKPRESSURE] ⚠️ WebSocket缓冲区达到 ${(socket.bufferedAmount/1024).toFixed(1)}KB，暂停10ms等待发送`);
                    await new Promise(resolve => setTimeout(resolve, 10));
                } else if (socket && socket.bufferedAmount > 32768) { // 32KB软阈值
                    // 轻度背压：微延迟1ms让缓冲区有机会清空
                    await new Promise(resolve => setTimeout(resolve, 1));
                }
                // 否则立即处理下一个chunk（无等待）
                // 这样可以在前台/后台都保持最大处理速度，同时避免缓冲区溢出
            }

        } catch (error) {
            // 🔍 诊断日志：详细记录错误信息
            const errorName = error.name || 'UnknownError';
            const errorMessage = error.message || String(error);
            
            console.error(`[REQUEST_LIFECYCLE] ❌ 请求 ${requestId.substring(0, 8)} 执行出错`);
            console.error(`[REQUEST_LIFECYCLE]   - 错误类型: ${errorName}`);
            console.error(`[REQUEST_LIFECYCLE]   - 错误信息: ${errorMessage}`);
            console.error(`[REQUEST_LIFECYCLE]   - 是否中止: ${errorName === 'AbortError'}`);
            console.error(`[REQUEST_LIFECYCLE]   - WebSocket状态: ${socket ? socket.readyState : 'NO_SOCKET'}`);

            // 如果错误是 AbortError，说明是主动取消，是正常流程
            if (errorName === 'AbortError') {
                console.log(`[REQUEST_LIFECYCLE] 🛑 请求 ${requestId.substring(0, 8)} 已被中止: ${error.message}`);
                
                // 🔧 关键修复：检查是否是服务器发起的取消
                const requestInfo = activeRequests.get(requestId);
                const isCancelledByServer = requestInfo && requestInfo.cancelled;
                
                if (isCancelledByServer) {
                    console.log(`[REQUEST_LIFECYCLE]   - 这是服务器发起的取消，不发送任何数据回服务器`);
                    // 服务器已经知道要取消了，不需要再发送[DONE]
                } else {
                    console.log(`[REQUEST_LIFECYCLE]   - 这是本地发起的取消，发送[DONE]通知服务器`);
                    sendToServer(requestId, "[DONE]");
                }
                return;
            }

            // 对于其他网络错误，尝试重试
            const errorMsg = error.message || String(error);
            const shouldRetry = (
                retryCount < MAX_RETRIES &&
                (errorMsg.includes('NetworkError') ||
                 errorMsg.includes('Failed to fetch') ||
                 errorMsg.includes('502') ||
                 errorMsg.includes('503') ||
                 errorMsg.includes('504'))
            );

            if (shouldRetry) {
                const delay = Math.min(BASE_DELAY * Math.pow(2, retryCount), MAX_DELAY);
                console.log(`[API Bridge] ⏳ 网络错误，等待 ${delay/1000} 秒后重试...`);
                
                sendToServer(requestId, { retry_info: { attempt: retryCount + 1, max_attempts: MAX_RETRIES, delay: delay, reason: error.message } });

                try {
                    await new Promise((resolve, reject) => {
                        const timeoutId = setTimeout(resolve, delay);
                        abortController.signal.addEventListener('abort', () => {
                            clearTimeout(timeoutId);
                            reject(new DOMException('Retry delay aborted', 'AbortError'));
                        });
                    });
                } catch (abortError) {
                    if (abortError.name === 'AbortError') {
                        console.log(`[RETRY_CANCEL] 🛑 网络错误重试等待期间被取消: ${requestId.substring(0, 8)}`);
                        return; // 中止重试
                    }
                    throw abortError;
                }
                
                // 🔧 核心修复：在执行重试前再次检查取消状态
                const currentRequestInfo = activeRequests.get(requestId);
                if (currentRequestInfo && currentRequestInfo.cancelled) {
                    console.log(`[RETRY_CANCEL] 🛑 网络错误重试前检测到取消，中止重试: ${requestId.substring(0, 8)}`);
                    return;
                }
                
                await executeFetchAndStreamBack(requestId, payload, retryConfig, retryCount + 1);
                return;
            }

            // 重试耗尽或非可重试错误
            sendToServer(requestId, { error: error.message });
            sendToServer(requestId, "[DONE]");
        } finally {
            // 🔧 终极修复：清理 activeRequests 맵
            activeRequests.delete(requestId);
            console.log(`[REQUEST_LIFECYCLE] 🧹 已清理请求资源: ${requestId.substring(0, 8)}`);
            window.isApiBridgeRequest = false;
        }
    }

    // 根据目标位置过滤流数据 (从 main 版本复制)
    function filterStreamByTarget(chunk, targetPosition) {
        // 目标位置可以是 'a' 或 'b'
        // 创建正则表达式匹配目标位置的数据
        // 例如：a0:"..." 或 ad:{...} 用于位置 'a'
        // 例如：b0:"..." 或 bd:{...} 用于位置 'b'
        const pattern = new RegExp(`${targetPosition}[0d2]:[^\\n]*`, 'g'); // 匹配 0(文本), d(结束), 2(图片)
        const matches = chunk.match(pattern);
        
        if (matches && matches.length > 0) {
            // 返回所有匹配项，用换行符连接
            return matches.join('\n') + '\n';
        }
        
        return null; // 没有匹配的内容
    }

    function sendToServer(requestId, data) {
        // 🔧 关键修复：在发送前检查请求是否已被取消
        const requestInfo = activeRequests.get(requestId);
        if (requestInfo && requestInfo.cancelled) {
            console.log(`[REQUEST_LIFECYCLE] 🚫 请求已取消，拒绝发送数据: ${requestId.substring(0, 8)}`);
            return; // 不发送任何数据
        }
        
        if (socket && socket.readyState === WebSocket.OPEN) {
            const message = {
                request_id: requestId,
                data: data
            };
            socket.send(JSON.stringify(message));
        } else {
            console.error("[API Bridge] 无法发送数据，WebSocket 连接未打开。");
        }
    }

    // --- 网络请求拦截 ---
    const originalFetch = window.fetch;
    window.fetch = function(...args) {
        const urlArg = args[0];
        let urlString = '';

        // 确保我们总是处理字符串形式的 URL
        if (urlArg instanceof Request) {
            urlString = urlArg.url;
        } else if (urlArg instanceof URL) {
            urlString = urlArg.href;
        } else if (typeof urlArg === 'string') {
            urlString = urlArg;
        }

        // 仅在 URL 是有效字符串时才进行匹配
        if (urlString) {
            const match = urlString.match(/\/nextjs-api\/stream\/retry-evaluation-session-message\/([a-f0-9-]+)\/messages\/([a-f0-9-]+)/);

            // 仅在请求不是由API桥自身发起，且捕获模式已激活时，才更新ID
            if (match && !window.isApiBridgeRequest && isCaptureModeActive) {
                const sessionId = match[1];
                console.log(`[API Bridge Interceptor] 🎯 在激活模式下捕获到 session ID！正在发送...`);

                // 关闭捕获模式，确保只发送一次
                isCaptureModeActive = false;
                if (document.title.startsWith("🎯 ")) {
                    document.title = document.title.substring(2);
                }

                // 异步将捕获到的ID发送到本地的 id_updater.py 脚本
                const captureData = JSON.stringify({ sessionId });
                
                // 发送到id_updater.py（5103端口）
                fetch('http://127.0.0.1:5103/update', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: captureData
                })
                .then(response => {
                    if (!response.ok) throw new Error(`Server responded with status: ${response.status}`);
                    console.log(`[API Bridge] ✅ Session ID 更新成功发送。捕获模式已自动关闭。`);
                })
                .catch(err => {
                    console.error('[API Bridge] 发送ID更新时出错:', err.message);
                    // 即使发送失败，捕获模式也已关闭，不会重试。
                });
            }
        }

        // 调用原始的 fetch 函数，确保页面功能不受影响
        return originalFetch.apply(this, args);
    };


    // --- 页面源码发送 ---
    async function sendPageSource() {
        try {
            const htmlContent = document.documentElement.outerHTML;
            await fetch('http://localhost:5102/internal/update_available_models', { // 新的端点
                method: 'POST',
                headers: {
                    'Content-Type': 'text/html; charset=utf-8'
                },
                body: htmlContent
            });
             console.log("[API Bridge] 页面源码已成功发送。");
        } catch (e) {
            console.error("[API Bridge] 发送页面源码失败:", e);
        }
    }

    // --- 启动连接 ---
    console.log("========================================");
    console.log("  LMArena API Bridge v2.9 正在运行。");
    console.log(`  📋 标签页ID: ${TAB_ID}`);
    console.log("  ✅ 使用新的 post-to-evaluation API");
    console.log("  ✅ 只需要 session_id (019开头的UUID v7)");
    console.log("  ✅ 支持 Direct 和 Battle 模式");
    console.log("  ✅ 支持多标签页并发");
    console.log("  ✅ 自动重试机制处理空响应");
    console.log("  - 聊天功能已连接到 ws://localhost:5102");
    console.log("  - ID 捕获器将发送到 http://localhost:5103");
    console.log("========================================");

    connect(); // 建立 WebSocket 连接

})();
