export const meta = {
  name: 'lmarena-bridge-full-audit',
  description: '全面审计 LMArenaBridge 项目的 bug、体验问题、安全与并发缺陷，并对每条发现做对抗性验证',
  phases: [
    { title: 'Audit', detail: '12 个维度并行深度审计（后端/前端/安全/并发）' },
    { title: 'Verify', detail: '对每个维度的发现做对抗性证伪' },
  ],
}

const ROOT = 'A:/api/LMArenaBridge-ModifiedVersion-11-5'

const COMMON = [
  '你正在审计一个 Python FastAPI 项目：LMArenaBridge-ModifiedVersion（根目录 ' + ROOT + '）。',
  '它把 LMArena 网页版和多家直连 API（Anthropic / Gemini / OpenAI 兼容）桥接成 OpenAI/Anthropic/Gemini 兼容 API，',
  '带有 admin 管理面板（admin.html + js/*.js）、监控页（monitor.html）、登录页（login.html）、SQLite 统计、API Key 管理、负载均衡、流式 SSE 处理、油猴脚本浏览器端。',
  '',
  '【硬性范围约束】',
  '- 只审计工作区当前代码。完全忽略 归档/ 目录、__pycache__/、.git/、node_modules/、logs/、downloaded_images/、deepseek_v3_tokenizer/ 以及 tokenizers/ 下的第三方数据文件。',
  '- 忽略根目录下 _test_*.py、_diag_*.py、_inspect_*.py、_scan_*.py 这些一次性脚本自身的质量问题（但可以读它们来理解设计意图）。',
  '',
  '【你要找什么】',
  '1. 真 bug：导致错误结果、异常、崩溃、数据错乱、状态不一致的缺陷。含边界条件、None/KeyError/IndexError、类型不匹配、off-by-one、异常被吞、缺失的 await、错误的 early return、变量遮蔽。',
  '2. 资源泄漏 / 并发问题：未关闭的 aiohttp/httpx session 或响应、未取消的 asyncio task、未释放的锁、队列无界增长、同步阻塞 IO 出现在 async 函数里、竞态、迭代时修改容器、WebSocket 生命周期错误。',
  '3. 安全问题：鉴权绕过、路径穿越、SSRF、命令注入、前端 XSS（innerHTML 拼接未转义）、密钥/token 泄漏进日志或响应、CORS 过宽、会话/Cookie 缺陷、时序攻击。',
  '4. 体验问题 (UX)：错误信息不可读或被静默吞掉、前端交互死角（按钮无反馈、缺加载态、失败不提示）、配置项不生效、日志噪音过大、admin 面板功能失效、边缘情况把用户卡死。',
  '5. 逻辑/一致性缺陷：同一份逻辑在多处实现且行为不一致、配置项读取路径不一致、默认值互相矛盾、文档与实现不符。',
  '',
  '【你不要报什么】',
  '- 纯风格 / 命名 / 类型注解缺失 / 格式化问题。',
  '- "建议补测试" 这类泛泛建议。',
  '- 说不出具体触发路径的理论隐患。',
  '- 已被上层调用者正确处理掉的情况——先读调用方再下结论。',
  '',
  '【工作方法】',
  '- 必须实际打开并读完你负责的文件，不要凭文件名猜测。',
  '- 每个发现都要给出：确切的 file 与 line、能证明问题的代码摘录、具体触发场景（什么请求/什么配置/什么时序会踩到）、可直接落地的修复方案。',
  '- 追踪调用链：可疑点要去看它的调用者和被调用者，确认不是误报。用 Grep 找全部引用。',
  '- 按严重度排序，最多返回 10 条，宁缺毋滥——每条都必须是你敢打赌真实存在的问题。',
].join('\n')

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string', description: '本维度内唯一的短 id，如 F1、F2' },
          title: { type: 'string', description: '一句话问题标题' },
          file: { type: 'string', description: '相对项目根的文件路径' },
          line: { type: 'integer', description: '1-indexed 行号' },
          severity: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          category: { type: 'string', enum: ['bug', 'security', 'resource-leak', 'concurrency', 'ux', 'consistency', 'perf'] },
          description: { type: 'string', description: '缺陷是什么，为什么错' },
          evidence: { type: 'string', description: '证明问题的实际代码摘录' },
          repro: { type: 'string', description: '具体触发场景：什么请求/配置/时序会踩到，产生什么错误结果' },
          fix: { type: 'string', description: '可落地的修复方案，尽量具体到改哪几行、改成什么' },
        },
        required: ['id', 'title', 'file', 'line', 'severity', 'category', 'description', 'evidence', 'repro', 'fix'],
      },
    },
  },
  required: ['findings'],
}

const VERDICT_SCHEMA = {
  type: 'object',
  properties: {
    verdicts: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          real: { type: 'boolean', description: '经过独立复核，这个缺陷是否真实存在且值得修' },
          confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
          reason: { type: 'string', description: '证伪或确认的依据，要引用你实际读到的代码' },
          severity_corrected: { type: 'string', enum: ['critical', 'high', 'medium', 'low'] },
          fix_corrected: { type: 'string', description: '如果原修复方案有误或不完整，给出修正后的方案；否则重述确认无误的方案' },
        },
        required: ['id', 'real', 'confidence', 'reason', 'severity_corrected', 'fix_corrected'],
      },
    },
  },
  required: ['verdicts'],
}

const DIMENSIONS = [
  {
    key: 'direct-api-core',
    scope: '直连 API 的核心调度与会话层：routes/direct_api_handler.py、routes/_direct_api_utils.py、routes/_direct_api_stream_session.py、routes/_direct_api_reasoning_cache.py、services/direct_api_service.py',
    focus: '重点看：上游 HTTP 会话的创建与关闭是否配对、流式会话中断/客户端断连时的清理、重试与降级逻辑、reasoning 缓存的键设计与失效、超时传播、错误码映射、上游非 200 响应体的读取与转发。',
  },
  {
    key: 'direct-api-anthropic',
    scope: 'Anthropic 直连与格式转换：routes/_direct_api_anthropic.py（1500+ 行）、converters/anthropic_openai.py（1100+ 行）',
    focus: '重点看：/v1/messages 原生协议实现、thinking/adaptive thinking 块处理、tool_use 与 tool_result 往返转换、content block 索引与 stop_reason、system prompt 处理、图片 block、SSE 事件序列完整性（message_start/content_block_delta/message_delta/message_stop 是否配平）、usage 统计、OpenAI 与 Anthropic 双向转换的信息丢失。',
  },
  {
    key: 'direct-api-gemini-passthrough',
    scope: 'Gemini 直连与透传：routes/_direct_api_gemini.py、routes/_direct_api_passthrough.py、routes/gemini_v1beta_api.py',
    focus: '重点看：Gemini v1beta 协议映射、safety settings、function calling、透传模式下 header 与 body 的过滤（是否泄漏内部密钥或注入上游 header）、路径参数校验、流式与非流式分支的一致性、错误结构转换。',
  },
  {
    key: 'streaming',
    scope: '流式处理管线：services/stream_processor.py、services/stream_parsers.py、services/stream_formatters.py、utils/json_unescape.py',
    focus: '重点看：SSE 分帧与缓冲（跨 chunk 半行、\\r\\n 处理、超长行）、多字节 UTF-8 在 chunk 边界被截断、增量 JSON 解析、tool call arguments 的转义/反转义正确性、生成器异常处理与 finally 清理、背压、最后一帧与 [DONE] 的发送、usage 累计。',
  },
  {
    key: 'openai-routes-ws',
    scope: 'OpenAI 兼容路由与 LMArena WebSocket 通道：routes/api_routes.py、routes/models_api.py、routes/lmarena_handler.py、routes/websocket_routes.py、background_tasks/monitors.py、background_tasks/request_processor.py',
    focus: '重点看：请求 id 与响应通道的映射（是否可能串号/内存泄漏）、WebSocket 断线重连与待处理请求的处置、后台任务的异常是否会静默终止任务、队列与超时、模型列表合并与去重、并发请求下的全局状态写入。',
  },
  {
    key: 'core-app',
    scope: '应用骨架与核心设施：api_server_new.py、core/app_state.py、core/config_loader.py、core/constants.py、core/errors.py、core/load_balancer.py、core/logging_config.py、core/middleware.py、core/web_session.py、core/api_key_manager.py、core/db_stats.py、utils/jsonc_edit.py、utils/task_registry.py、utils/api_helpers.py',
    focus: '重点看：启动/关闭生命周期（lifespan 中资源是否成对释放）、配置热重载的线程安全与默认值、jsonc 编辑是否会破坏用户注释或写坏文件、middleware 的鉴权顺序与豁免路径、web session 的 cookie 安全属性与过期、负载均衡的选路与失败计数、api key 轮换与并发访问、SQLite 连接的线程/协程安全与事务。',
  },
  {
    key: 'admin-backend',
    scope: '管理后端路由：routes/admin_routes.py（1300+ 行）、routes/apikey_routes.py、routes/auth_routes.py、routes/monitor_routes.py、routes/internal_routes.py',
    focus: '重点看：每个端点的鉴权覆盖（有没有漏挂依赖的端点）、写配置/写文件端点的输入校验与路径穿越、密码校验是否恒定时间、登录失败限流、内部路由是否对外暴露、返回体是否泄漏密钥明文、批量操作的部分失败处理。逐个端点列举并核对鉴权，这是重点。',
  },
  {
    key: 'modules-monitoring-media',
    scope: '监控与媒体模块：modules/monitoring.py（1400 行）、modules/monitoring_sqlite.py、modules/image_processor.py、modules/file_uploader.py、modules/token_counter/*、services/image_service.py、services/image_handler.py、services/message_converter.py、services/token_service.py、utils/monitor_params.py',
    focus: '重点看：监控写入是否阻塞请求路径、SQLite 并发写与 WAL、统计聚合的时区与日期边界、图片下载的大小/超时/类型校验与 SSRF、base64 与 data URI 解析、临时文件清理、tokenizer 加载失败的降级、token 计数与实际 usage 的偏差、消息转换中角色/多模态内容的丢失。',
  },
  {
    key: 'frontend-admin',
    scope: 'admin 前端：js/admin-core.js、js/admin-config.js、js/admin-apikeys.js、js/admin-overview.js、js/admin-charts.js、js/admin-models-list.js、js/admin-models-edit.js、js/admin-models-capture.js、js/admin-tokenizer.js、admin.html、css/admin.css',
    focus: '重点看：innerHTML 拼接未转义导致的 XSS（模型名/备注/key 名等用户可控字段）、fetch 失败与非 2xx 未提示用户、并发保存导致的覆盖、表单校验缺失、事件监听重复绑定、大列表渲染性能、未保存改动丢失、分页/筛选状态错乱、图表数据为空时崩溃、深色模式与响应式的明显破绽。',
  },
  {
    key: 'frontend-monitor-auth',
    scope: '监控页与登录页前端：js/monitor.js、monitor.html、login.html、token_calculator.html',
    focus: '重点看：轮询/SSE 的清理与重连、页面隐藏时是否仍在轮询、渲染大量记录的性能、请求详情展示中的 XSS、登录流程的错误提示与重定向、token 存储方式、计算器的边界输入。',
  },
  {
    key: 'security-crosscut',
    scope: '全项目横切安全审计（不限文件，但以 routes/、core/、modules/file_uploader.py、file_bed_server/main.py、js/ 为主）',
    focus: '系统性核查：1) 枚举所有 FastAPI 路由并逐一确认鉴权要求，找出未受保护却应受保护的端点；2) 所有把用户输入拼进文件路径/命令/URL 的地方（路径穿越、SSRF、命令注入）；3) 所有把密钥、cookie、Authorization 写进日志或返回给前端的地方；4) CORS 与 TrustedHost 配置；5) 前端所有 innerHTML/outerHTML/insertAdjacentHTML 的数据来源。给出具体端点名与行号。',
  },
  {
    key: 'concurrency-crosscut',
    scope: '全项目横切并发与资源审计（不限文件，重点 async 代码路径）',
    focus: '系统性核查：1) 所有 asyncio.create_task 是否保存引用并有异常处理（被 GC 静默丢弃）；2) 所有 aiohttp/httpx 客户端与响应是否在异常路径上也被关闭；3) 所有全局可变字典/集合在并发请求下的读改写竞态；4) 所有 async def 里的同步阻塞调用（open/requests/time.sleep/PIL/sqlite3 未走线程池）；5) 所有锁的获取顺序与是否可能死锁；6) 无界增长的缓存/队列/字典（内存泄漏）。给出具体位置与触发时序。',
  },
]

phase('Audit')

const results = await pipeline(
  DIMENSIONS,
  (d) => agent(
    COMMON + '\n\n【你负责的范围】\n' + d.scope + '\n\n【本维度重点】\n' + d.focus,
    { label: 'audit:' + d.key, phase: 'Audit', schema: FINDINGS_SCHEMA }
  ),
  (found, d) => {
    if (!found || !found.findings || found.findings.length === 0) return { key: d.key, verdicts: [], findings: [] }
    const list = found.findings.map((f) =>
      '--- ' + f.id + ' [' + f.severity + '/' + f.category + '] ' + f.title + '\n' +
      '位置: ' + f.file + ':' + f.line + '\n' +
      '描述: ' + f.description + '\n' +
      '证据: ' + f.evidence + '\n' +
      '触发: ' + f.repro + '\n' +
      '建议修复: ' + f.fix
    ).join('\n\n')
    return agent(
      '你是一名极度挑剔的代码复核员，负责证伪另一名审计员在项目 ' + ROOT + ' 中提出的缺陷报告。\n' +
      '你的默认立场是「这条大概率是误报」，只有当你亲自打开文件、读完相关代码与调用链、确认问题确实成立时，才判定 real=true。\n\n' +
      '【证伪时必须检查的常见误报模式】\n' +
      '- 上层调用者其实已经做了校验/兜底（try/except、依赖注入的鉴权、前置的 if 判断）。\n' +
      '- 该分支实际不可达，或被配置默认值排除。\n' +
      '- 审计员读错了行号 / 看的是 归档/ 里的旧代码 / 引用的函数其实是另一个同名函数。\n' +
      '- 框架本身已经处理（FastAPI 自动关闭响应、aiohttp 上下文管理器、Starlette 的异常处理）。\n' +
      '- 描述的"竞态"在单事件循环下其实不存在（没有 await 让出点）。\n' +
      '- 建议的修复方案本身是错的、会引入回归、或与项目现有约定冲突。\n\n' +
      '【判定要求】\n' +
      '- 必须实际读文件核对，禁止仅凭报告文本下结论。用 Read 打开 file 附近至少 60 行，用 Grep 找调用方。\n' +
      '- reason 里必须引用你亲眼读到的代码或调用点来支撑判定，不能空泛。\n' +
      '- 判定 real=true 时，重新评估严重度（审计员常常夸大），并检查其修复方案是否正确、是否会破坏现有功能；有问题就在 fix_corrected 里给出修正方案。\n' +
      '- 判定 real=false 时，说清楚是哪一处让它不成立。\n' +
      '- 拿不准的判 real=false 并标 confidence=low。\n\n' +
      '【待复核的发现（维度: ' + d.key + '，范围: ' + d.scope + '）】\n\n' + list,
      { label: 'verify:' + d.key, phase: 'Verify', schema: VERDICT_SCHEMA, effort: 'high' }
    ).then((v) => ({ key: d.key, findings: found.findings, verdicts: (v && v.verdicts) || [] }))
  }
)

const confirmed = []
const rejected = []
let totalRaw = 0

for (const r of results.filter(Boolean)) {
  const byId = {}
  for (const f of r.findings || []) byId[f.id] = f
  totalRaw += (r.findings || []).length
  for (const v of r.verdicts || []) {
    const f = byId[v.id]
    if (!f) continue
    const row = {
      dimension: r.key,
      id: r.key + ':' + v.id,
      title: f.title,
      file: f.file,
      line: f.line,
      category: f.category,
      severity: v.severity_corrected || f.severity,
      description: f.description,
      evidence: f.evidence,
      repro: f.repro,
      fix: v.fix_corrected || f.fix,
      confidence: v.confidence,
      verdict_reason: v.reason,
    }
    if (v.real) confirmed.push(row)
    else rejected.push({ id: row.id, title: row.title, file: row.file, reason: v.reason, confidence: v.confidence })
  }
}

const rank = { critical: 0, high: 1, medium: 2, low: 3 }
confirmed.sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9))

log('审计完成：原始发现 ' + totalRaw + ' 条，确认 ' + confirmed.length + ' 条，证伪 ' + rejected.length + ' 条')

return { confirmed, rejected, stats: { raw: totalRaw, confirmed: confirmed.length, rejected: rejected.length } }
