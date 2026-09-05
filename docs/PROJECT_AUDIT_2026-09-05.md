# 全项目审查、修复与兼容性报告

审查日期：2026-09-05。起点：`9e9b04b`，分支：`main`。

本文保留首轮审查阶段的证据与决策背景。用户确认后的实现、真实配置/数据库迁移和最终验证，见 [实施总报告](GATEWAY_IMPLEMENTATION_REPORT_2026-09-05.md)；下文“待决定”不再表示当前仍待批准。

本文保留首轮审查阶段的证据与决策背景。用户确认后的实现、真实配置/数据库迁移和最终验证，见 [实施总报告](GATEWAY_IMPLEMENTATION_REPORT_2026-09-05.md)；下文“待决定”不再表示当前仍待批准。

本次结论是：项目已经具备可继续维护的 FastAPI 分层结构，最需要加强的是配置事务、协议转换的保真程度、统一的上游执行策略和可验证的供应商能力描述。暂时没有证据支持为了这些问题整体更换后端框架。前端可以先完善状态管理和模块边界，再决定是否迁移 TypeScript 或 Vue。

## 1. 范围与证据边界

- 对仓库进行了文件清单、路由、调用关系、危险操作、阻塞操作、配置读写、缓存、前端异步更新和测试结构搜索，并逐段检查主要维护链路。中途统计快照为 102 个受版本控制的项目源码/页面文件，约 50,899 行；该数字排除了 vendor、外部 tokenizer 数据、油猴脚本和旧测试，不代表每一行都经过人工逐行证明。
- 重点覆盖 Chat Completions、Messages、Responses、Gemini GenerateContent / Interactions、管理面板、监控、统计、API Key、配置热更新、图片处理、独立图床和启动文档。
- 按 README 的维护约定，LMArena 浏览器/Battle/抓取功能作为退役代码审视，没有恢复维护或改变其业务流程。
- 没有启动或重启真实网关，没有主动编辑真实模型配置、密钥或执行业务数据迁移，没有调用付费上游做探测。早期基线测试在仓库目录运行；审查发现模块导入会初始化默认日志目录/数据库，不能把早期测试描述为完全隔离。最终增加会话级临时工作目录，完整测试在隔离环境通过；没有擅自清理原有业务目录。
- 官方文档已实际打开核对。文档兼容性、模拟传输契约通过和真实供应商运行成功是三种不同证据，本报告分别说明。
- 起始工作区已有未跟踪的 `utils/schema_sanitizer.py`；它被已提交代码引用。本次补录该现有文件以保证干净检出包含依赖，未改动其内容。它的默认策略仍需要产品决定，见第 6 节。

## 2. 已修复问题

P1 表示可能造成未授权调用、配置丢失或实质性数据错误；P2 表示功能或协议错误；P3 表示展示、部署或维护问题。以下按根因合并，避免把同一处修复的每个字段分别计数。

| 编号 | 级别 | 原问题与触发条件 | 实际修复 | 主要位置 / 验证 |
|---|---|---|---|---|
| A01 | P1 | 未配置管理员/访客 Key 时，外部请求随便携带一个 Key 就能绕过仅本机访问限制 | 把未配置认证的本机检查提前，不再受是否携带 Key 影响 | `routes/api_routes.py`；空 Key、Bearer、x-api-key 三组回归 |
| A02 | P1/P2 | Gemini 模型列表不接受官方常用鉴权方式，且向访客列出无权限模型 | 支持 x-goog-api-key、query key、Bearer；按访客白名单过滤 | `routes/models_api.py`；三种鉴权方式与名单回归 |
| A03 | P2 | JSON 数组、字符串、错误类型的 model/messages/stream 导致属性错误或错误分支；未知模型进入退役浏览器等待流程 | 请求形状返回 400，未知模型及时返回 404；管理接口统一验证对象请求体 | `routes/api_routes.py`、`routes/admin_routes.py`、`routes/apikey_routes.py` |
| A04 | P1 | 配置根节点写成数组时可能破坏当前字典；模型映射文件暂时消失会清空运行映射 | 替换前验证对象根节点；保留最后有效模型映射 | `core/config_loader.py`；原配置不变断言 |
| A05 | P1 | API Key 文件的无效根结构可能清空当前鉴权状态 | 重建索引前验证文件结构，出错保留原 Key 与索引 | `core/api_key_manager.py` |
| A06 | P1 | 较早的 Key 快照可能较晚写入，恢复已撤销 Key；手动 reload 与管理修改也可能交错 | 管理修改/重载串行化；取得写锁后重新获取最新快照；鉴权热路径不持有磁盘写锁 | `core/api_key_manager.py`；延迟旧快照回归 |
| A07 | P1 | 保存/归档持锁，但删除和排序未持同一把锁，读改写可能互相覆盖 | 统一模型文件事务锁覆盖删除、排序 | `routes/admin_routes.py`；等待锁时禁止提前读取 |
| A08 | P1 | 全局配置、自动归档设置、tokenizer 映射各自读改写同一 JSONC 文件，互相覆盖 | 三条管理写入链路共享 JSONC 事务锁 | `routes/admin_routes.py`；三个入口锁竞争回归 |
| A09 | P1 | 配置编辑器先改模式再判断旧模式，表单改动丢失；重复点当前模式也可能重建表单 | 保存旧模式、重复切换不操作、切换时同步未保存值并保留注释 | `js/admin-config.js`、`js/jsonc-utils.js`；Chromium 往返测试 |
| A10 | P2 | JSONC 解析器误改字符串内的 `,}`；反斜杠结尾字符串解析错误；前后端对尾逗号支持不一致 | 前端基于词元解析；后端共用字符串感知的 JSONC 解析器 | `js/jsonc-utils.js`、`utils/jsonc_edit.py`；路径/URL/注释/尾逗号回归 |
| A11 | P1/P2 | 数值 0 被 `||` 默认值覆盖；浅拷贝表单对象会修改已加载配置的嵌套对象 | 数值使用空值判断，表单收集采用深拷贝 | `js/admin-config.js`；表单快照不变回归 |
| A12 | P1 | 编辑多端点模型只保留首端点；重建配置会删除表单不认识的字段 | 保存原始配置快照，只替换表单管理的字段，保留其他端点及扩展字段 | `js/admin-models-workspace.js`、`js/admin-models-edit.js`；浏览器保存断言 |
| A13 | P2 | 打开模型后若它被其他操作删除，未改名的保存会把它重新创建 | 编辑保存始终携带 old_model_name，后端可检测原模型已不存在 | `js/admin-models-edit.js` |
| A14 | P2 | 未带内容指纹的 vendor 文件被缓存一年且 immutable；CSS 更新后也可能仍显示旧布局 | JS/CSS 统一允许缓存但要求重新验证 | `core/middleware.py` |
| A15 | P2 | 慢的旧日志查询覆盖新的筛选/分页结果 | 为请求分配序号，仅最新查询可以提交 UI 状态 | `js/monitor.js`；倒序完成的浏览器回归 |
| A16 | P2 | 监控标签切换依赖浏览器全局 event；费用 title 中的货币字符串未转义 | 通过显式 data-tab 找到标签；统一转义费用文本和属性 | `js/monitor.js`、`monitor.html`；无事件切换和属性注入回归 |
| A17 | P2 | CSV 费用列一律标 USD；声明 utf-8-sig 却没有实际 BOM；模型名称可能被电子表格解释为公式 | 列名标注原币，写入实际 BOM，对不可信文本单元格加文本前缀 | `routes/admin_routes.py`、`utils/csv_export.py` |
| A18 | P1 | 一个模型历史记录同时含 USD/CNY 时，模型汇总直接相加后贴上某一种货币标签 | 先按模型和货币分别聚合，再按当前汇率转换到展示货币；不改历史行 | `core/db_stats.py`；1 USD + 7.2 CNY = 2 USD / 14.4 CNY 回归 |
| A19 | P2 | 自动归档的 SQLite 聚合查询直接运行在事件循环 | 将最后使用时间统计移到线程池 | `routes/admin_routes.py` |
| A20 | P2 | SSE 把每一行 data 当独立 JSON，合法多行事件被丢弃；数组 payload 会触发 `.get` 异常 | 按事件边界合并 data 行、检查对象类型、处理最后无空行事件 | `services/sse.py`；UTF-8 逐字节、CRLF、多行、DONE、尾块回归 |
| A21 | P2 | 超大 SSE 单行每次收到分块都复制累计字符串，接近二次复杂度 | 使用分片列表累计未完成行，完整时才拼接；从大服务中提取解析器 | `services/sse.py`；见性能实测 |
| A22 | P1/P2 | Gemini thoughtSignature 实际在 Part 层，原代码从 functionCall 内读取；历史回传时又丢弃签名 | 从正确层级提取并通过 `_thought_signature` 回传到 Part | `services/direct_api_service.py`；工具请求/响应往返 |
| A23 | P2 | Gemini 不同流分块中的工具调用都可能从 index=0 开始，客户端把两个调用拼成一个 | 每次流请求共享调用 ID 到连续 index 的映射 | `services/direct_api_service.py`、`routes/_direct_api_gemini.py` |
| A24 | P2 | Gemini 同时输出文本和工具调用时丢失文本；STOP 被映射为普通结束而非工具调用结束 | 保留文本，并根据本轮工具状态给出 tool_calls 完成原因 | `services/direct_api_service.py` |
| A25 | P2 | Chat 到 Gemini GenerateContent 转换忽略 response_format 和 stop | 增加 responseMimeType / responseJsonSchema / stopSequences 映射，流式和非流式共用 | `services/direct_api_service.py`、`routes/_direct_api_gemini.py` |
| A26 | P1/P2 | Anthropic 思考设置覆盖或删除整个 output_config，破坏结构化输出格式 | 提取共享 effort 修改函数，只修改 effort，保留 format 等其他字段 | `utils/anthropic_params.py`；两个入口、五种思考配置组合 |
| A27 | P2 | Anthropic 与 Chat 转换遗漏 strict、并行工具控制、JSON Schema 和返回方向的 effort | 增加相应明确字段映射 | `converters/anthropic_openai.py`、`routes/_direct_api_anthropic.py`；契约往返 |
| A28 | P2 | 原生 Responses 请求仅浅拷贝，schema 清洗可能修改调用方原始嵌套对象 | 原生请求转换使用独立深拷贝 | `routes/responses_api.py` |
| A29 | P1/P2 | Responses TCP 正常 EOF 被当成请求成功，即使没有协议终态事件 | 处理尾部事件后检查终态；缺失时发出流内 error 并记录失败 | `routes/responses_api.py`；截断流和完整尾块回归 |
| A30 | P2 | Chat content_filter 被转成 Responses completed | 两种输出模式都转为 incomplete，并保留 content_filter 原因 | `converters/responses_openai.py` |
| A31 | P2 | 图床白名单拒绝的 HTTP 400 被通用 except 包成 500；非法 base64 可能被宽松接受 | 保留 HTTPException，严格验证 base64 | `file_bed_server/main.py`；真实 FastAPI TestClient |
| A32 | P2 | 图床清除元数据后丢失 Image.format，导致优化失败回退；把像素转成 Python list 放大内存 | 提前保存格式，使用图像 copy 并清除 info，保留像素和调色信息 | `file_bed_server/main.py`；PNG 尺寸、格式、像素和元数据验证 |
| A33 | P2 | 图床 async 端点内部全是同步图片和磁盘工作，阻塞事件循环 | 改为同步 FastAPI 端点，由框架线程池运行 | `file_bed_server/main.py` |
| A34 | P2/P3 | 图床 JSONC 加载仅删除整行注释；独立依赖文件漏掉 Pillow | 复用后端 JSONC 解析器；补齐 Pillow 依赖 | 图床代码与 requirements |
| A35 | P2/P3 | 已提交的 schema 清洗调用依赖一个未跟踪文件 | 将已有依赖文件纳入版本控制，保持其内容不变 | `utils/schema_sanitizer.py` |
| A36 | P3 | README 初始化命令引用不存在的示例文件，结构树含不存在模块，Responses 能力说明过时 | 修正文档；新增开发测试依赖清单 | `README.md`、`requirements-dev.txt` |
| A37 | P2 | 测试导入网关模块时可能初始化仓库中的真实运行状态目录 | 在收集测试之前切换到独立临时工作目录，仓库只用于读取源码和页面资产 | `tests/conftest.py`；最终完整测试在该环境通过 |

补充边界：A08 的锁保护同一进程内的管理写入，不会阻止用户在外部编辑器改文件，也不解决多进程共享 JSON 的事务问题。A18 延续现有每日统计的“按当前汇率展示”规则，按历史汇率核算是另一项需求。A22 需要客户端保留扩展签名字段；会丢弃该字段的客户端仍应使用 Gemini 原生协议，或增加显式的会话状态服务。

## 3. 验证与性能

基线：167 个测试通过。新增覆盖包括真实 Chromium 管理/监控页面、临时 SQLite 统计、图床 FastAPI 请求、协议转换、伪上游流、配置竞争和只读工具样例。最终完整结果记录在本文件收尾的验证记录中。

性能实验用本次起始提交中的旧 SSE 函数和新函数处理同一合法 JSON 事件；网络分块均为 1,024 bytes，每项运行三次取中位数。只测本地 CPU 解析，不含真实网络、供应商排队、tokenizer、持久化或端到端并发压测。

| 单事件正文 | 旧函数 | 新函数 | 解释 |
|---|---:|---:|---|
| 1 MiB | 13.89 ms | 2.60 ms | 避免反复复制未完成行 |
| 4 MiB | 556.10 ms | 9.98 ms | 大图/base64 事件受益尤其明显 |

运行方式：

```powershell
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest tests -q
```

`tests/legacy/_test_*.py` 不会被默认 pytest 收集。其中存在导入即执行的旧脚本和与历史实现绑定的断言，不能把它们算进本次通过数，也不适合未经隔离直接在生产工作目录批量运行。建议逐个把仍有价值的断言迁入正常测试。

## 4. 体验、前端和性能的后续优化

| 方向 | 现有证据 | 建议与取舍 |
|---|---|---|
| 配置表单默认值 | `js/admin-config.js` 中展示默认值与收集函数的部分默认值不同，后端也有自己的默认值 | 建立后端配置 schema，返回 effective_config 与原始覆盖值；表单只提交改动字段。默认值会改变运行语义，应统一确认后实施 |
| 跨浏览器配置冲突 | A08 只能串行化提交，不能发现用户基于旧版本编辑了很久 | GET 返回 revision，POST 携带 expected_revision，冲突返回 409 并显示差异。比静默“最后保存者覆盖”更适合多人/多标签页 |
| 多端点编辑体验 | 当前表单只能编辑首端点，A12 只保证其他端点不被丢弃 | 增加端点列表、单端点编辑和切换策略；先明确这是负载均衡还是主备故障转移 |
| 配置草稿 | 切到其他页面会重新加载配置，没有全局配置页的完整离开保护 | 内存草稿、显式放弃/恢复、保存期间禁用重复提交；若要跨重启恢复，需决定能否把含密钥草稿写到浏览器存储 |
| 监控详情体量 | `renderTruncatable` 仍把隐藏全文放进 HTML；请求详情包含可能很大的上下文 | 展开时再创建文本节点，关闭时清理原文引用；更进一步按块从详情 API 加载。应测 1/10/50 MB 样本而非只看小请求 |
| 监控轮询 | WebSocket 推送之外，还有多组 1/3/5/10 秒轮询 | 页面活动状态统一管理；活跃时长用本地时钟刷新；连接恢复时拉取快照。保留低频补偿以应对漏事件 |
| 错误体验 | 多处错误由 console、alert、Toast、空列表分别呈现 | 区分认证失效、权限不足、无数据、请求失败、旧数据；保留旧结果并显示“刷新失败”，不要把错误表现为空数据 |
| 可访问性 | 模型编辑器已有键盘处理，但其他页面仍有 div 点击区和内联 onclick | 改为 button 与显式标签、焦点恢复、aria-expanded；补键盘操作和 360 px 小屏回归 |
| 日志搜索 | SQLite 查询每次执行 COUNT 与 LIMIT/OFFSET；模糊搜索是 `%term%` | 大日志考虑 `(timestamp, request_id)` 游标分页；全文搜索用 FTS；先用真实规模的匿名数据验证，不凭索引数量判断性能 |
| 统计查询 | 一次管理统计要跑多次聚合；本次为正确处理混合币种增加一次分组查询 | 量大时使用按日/模型的汇总表，由单写入通道维护，并明确补算机制；不能直接缓存到永久不更新 |
| 日志排队 | `modules/monitoring.py` 在队列满时回退为同步写入 | 对请求记录保留可靠写入，对低优先级诊断日志采样；还是直接背压需结合“日志完整性/延迟”偏好决定 |
| 图片处理并发 | 多处使用线程池，但图片大小、任务数和缓存之间缺少统一预算 | 增加像素、下载字节、并发处理数量三种独立限制；大图可改为上传引用，避免多次 base64 复制 |
| Tokenizer | 支持自定义 tokenizer、远程代码与在线安装 | 不应为每次管理查看触发加载；添加加载状态、失败原因、版本信息。是否禁用远程代码必须由主人选择 |
| 可观测性 | 现有请求耗时不能完全分离连接池等待、首字节、首 token、生成和日志耗时 | 增加阶段计时与连接池状态，支持按协议/上游/错误码统计；先得到证据再调整连接池大小 |

## 5. 可部分重构的边界

本次已经提取 SSE 解析、前端 JSONC、共享后端 JSONC、Anthropic effort 修改、CSV 文本防护。它们都是当前缺陷需要的局部共用逻辑。

仍超过约 1,200 行的主要文件包括 `routes/admin_routes.py`、`routes/_direct_api_anthropic.py`、`services/direct_api_service.py`、`modules/monitoring.py`、`converters/gemini_interactions.py`、`js/admin-tokenizer.js`、`js/monitor.js`。行数只是需要检查边界的信号，不应为缩短文件而随机搬代码。

建议的依赖方向：

```text
HTTP 路由（认证、参数、响应状态）
  → 请求执行器（选端点、选 Key、重试、取消、生命周期）
    → 协议适配器（build_request、parse_event、finish）
      → HTTP transport（连接池、超时、首块预读、关闭）
  → 监控记录器（一次开始、一次结束、异步持久化）
```

具体拆分：

- `admin_routes.py` 拆为 models/config/stats/tokenizers/key_diagnostics 路由，但配置写入仍由同一个 repository 负责，不能每个模块重新造锁。
- `direct_api_service.py` 提取 Gemini 请求构造与响应转换。网络传输不再知道前端展示名、账单显示策略或 tokenizer。
- `api_routes.py` 的 Anthropic 原生链路与 `_direct_api_anthropic.py` 共享策略应用；保留原生透传和跨协议转换的差异，不用一个万能字典掩盖协议语义。
- `monitoring.py` 分成 request_tracker、log_writer、statistics_reader，显式传递不可变完成记录，避免运行状态和历史数据在多个线程间互相修改。
- `gemini_interactions.py` 拆成请求转换、事件累积、GenerateContent 适配。以同一个复杂工具往返 fixture 验证三个边界。
- 前端先改 ES modules，减少全局变量、onclick 字符串和跨文件暗依赖；引入 API client、模型草稿状态和请求序号管理。只有决定继续扩展多端点编辑、权限和资源管理后，再投入 TypeScript/Vue 迁移。

不建议本轮直接换 React/Vue、把 FastAPI 换成别的框架、上 Redis 或微服务。当前瓶颈主要是行为边界和数据一致性，更换框架并不能自动解决。

## 6. 已确认但需要集中选择方案的问题

下面不是“未发现问题”，而是问题/缺口已明确，解决方式会改变默认行为、权限边界或存储方式，因此没有擅自选择。

| 编号 | 问题 | 推荐方案 | 需要决定的部分 |
|---|---|---|---|
| D01 | schema 清洗默认开启，既截断递归又补齐 required，并把 strict=true 改为 false；对支持原始 schema 的官方服务会改变语义 | 官方服务默认保真，兼容清洗改为按上游显式开启；拆开递归、strict、required 三项策略 | 是否改变现有模型的默认行为；是否迁移已有配置 |
| D02 | 不同入口执行策略不一致：原生 Messages/Responses 单独选择 round-robin Key，Chat 调度器有 sticky/重试；流式错误也常不能触发外层重试 | 统一首块预读和执行器；在任何响应发给客户端前重试，已输出后只报错，不自动重新生成 | 允许重试哪些请求/状态；是否容许额外费用；全部 Key 不可用时返回失败还是排队 |
| D03 | sticky 把 429 与余额不足都视为配额耗尽，可能冷却 48 小时；自动重试已判定可重试时，现有条件可能跳过 sticky 冷却 | 429 按 Retry-After/短暂限流处理，明确余额耗尽才长期冷却；统一 retry 与 key rotation 的状态机 | 保留旧冷却时长还是迁移；怎样识别各聚合商的配额错误 |
| D04 | reasoning_details 缓存仅以思考文本 hash 为键，跨用户/模型共用，并会在未命中时剥离思考字段 | 以调用方、上游、模型、会话共同限定缓存；显式保存/回传签名优先于猜测恢复 | 老客户端如何建立稳定会话身份；能否保留服务端状态、保留多久 |
| D05 | Key/统计等文件写入失败有的仅日志警告，内存状态与磁盘可能不一致；JSON 管理锁只对本进程有效 | 配置变更返回持久化结果，建立版本/审计记录；长期把 Key 和资源归属放入事务数据库 | 写入失败时内存回滚还是保留变更等待重试；是否允许数据库迁移 |
| D06 | 网关尚无统一上传/下载大小预算；独立图床在未配置 FILE_BED_API_KEY 时允许无认证上传；自定义 tokenizer 使用 trust_remote_code | 上传限制、管理员专用安装入口、可信 tokenizer 来源；图床默认限制为本机或要求 Key | 限额数值、远程 tokenizer 是否继续允许、无 Key 的现有部署是否继续支持 |
| D07 | 当前管理会话保护与访客 API 权限不是完整多租户系统；统计/日志多以模型维度汇总 | 新增稳定的调用方 ID 并贯穿开始记录、完成记录、SQLite、查询和导出 | 是单人网关还是多人服务；访客能看到自己的成本还是完全不开放监控 |
| D08 | 历史成本按当前配置汇率展示；价格配置变更与历史重算的边界容易误解 | 保存每次请求的价格/汇率版本；历史金额与“按当前价格估算”分开显示 | 是否追溯重算旧数据，以及需要何种历史币种口径 |

建议主人优先决定 D01、D02/D03 和 D07，它们决定后续兼容层、执行器和工具权限设计。其余决定可以随对应功能一起做。

## 7. 供应商兼容性矩阵

“原生透传可携带”指当前路由能够转发该协议请求，不意味着本网关实现了供应商资源管理，也不意味着所有模型都支持该功能。模型名、区域、账户能力和价格应由供应商当前文档/实际账户决定，不能根据名称猜测。

| 供应商 | 当前可用路径 | 本次补齐 / 主要缺口 | 添加方式 |
|---|---|---|---|
| OpenAI | Chat 兼容、Responses 原生；Responses 到 Chat 的无状态转换 | 原生可携带内置工具与 reasoning items；缺 response 查询/取消/删除/input_items、Conversations、Files、Batch、Embeddings、Audio、Images、Realtime 专用路由 | 原生工具优先走 responses_native；持久资源必须绑定调用方和实际上游 Key；新增路由不能把请求随意轮询到另一账号。[Responses 参考](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) |
| Anthropic | `/v1/messages` 原生及 Chat 双向转换 | 本次增加结构化输出、strict、并行工具控制，并防止 thinking 设置删除格式；仍缺 count_tokens、Files、Message Batches 及服务端工具的跨协议映射 | 原生请求保留内容块和签名；非 function 的 server tools 不应降级为普通函数。扩展 count_tokens 最适合作为下一项小功能。[结构化输出](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)、[思考控制](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost) |
| Google Gemini | GenerateContent 原生、Chat 转换、配置选择 Interactions | 本次修复签名和工具分块，补结构化输出/stop；缺 Interactions 资源生命周期、Files、显式缓存、Live API 与完整音视频/图片输出桥接 | 文本工具调用保留 Part 签名；原生多模态不要压成文本。区分 GenerateContent 和 Interactions 字段。[GenerateContent](https://ai.google.dev/api/generate-content)、[签名规则](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures) |
| DeepSeek | Chat 兼容；按上游协议配置 Responses/Anthropic | 参数大多已可通过透传发送；带 tools 时历史 reasoning_content 必须完整回传，现有清理/兼容策略需要特别检查 | 使用 provider profile 标记思考历史要求；保留原始 assistant 消息，不按“上一轮已结束”清掉 reasoning。温度等在思考模式下可能不起作用，应由能力说明告知。[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/) |
| Qwen / 百炼 | OpenAI 兼容 Chat；可配置原生 Responses 上游 | enable_thinking、thinking_budget 可经额外参数发送；缺区域/工作空间配置向导与模型能力检查 | 不把统一思考开关机械转换成其他厂商字段。UI 按 profile 提供 enable_thinking 等真实字段；保留自定义 JSON。[百炼思考文档](https://www.alibabacloud.com/help/en/model-studio/deep-thinking) |
| xAI | Chat 兼容 / Responses 原生 | Responses 原生可携带 web_search、x_search、code_interpreter 等；Chat 转换无法完整表达内置工具、引用和多种输出 | 有内置工具的请求保持 Responses 全程原生，增加引用/工具执行统计；图片、视频、语音需要独立接口。[工具文档](https://docs.x.ai/developers/tools/overview) |
| Kimi | OpenAI 兼容 Chat | function tool 基础路径可用；strict 不能被网关默认清洗悄悄关闭；官方工具执行和资源管理没有统一入口 | 使用模型能力 profile；保留工具前的说明文本和工具调用序列。官方工具与用户本地函数分开处理。[Tool Use](https://platform.kimi.ai/docs/api/tool-use) |
| MiniMax | OpenAI 兼容 / Anthropic 兼容 | 原生 Anthropic 适合保留内容块；不能假设它支持 Anthropic 的所有字段和服务端工具 | 配置实际 base_url、版本路径及受支持参数；增加同协议不同供应商的契约 fixture。[Anthropic 兼容文档](https://platform.minimax.io/docs/api-reference/text-anthropic-api) |
| 智谱 GLM | OpenAI 兼容 Chat | 基础对话和函数调用结构可通过现有透传链路；独立服务、异步任务和平台工具未实现通用管理 | 普通调用保留原生扩展参数；异步资源按 provider 适配，不借用 Chat 完成事件。[HTTP API](https://docs.bigmodel.cn/cn/guide/develop/http/introduction) |
| OpenRouter | OpenAI 兼容透传，已有 reasoning_details 缓存 | provider 路由字段可透传；严格参数支持和签名缓存隔离需要改进 | 保留 provider.order/require_parameters 等显式控制，诊断报告区分网关选路和 OpenRouter 的下游选路。[Provider Routing](https://openrouter.ai/docs/guides/routing/provider-selection) |
| Mistral | OpenAI 兼容基础调用 | Agents/Conversations、Document AI、Embeddings、Audio 等不是现有 Chat 路由的自然扩展 | 普通函数走 Chat；Agents 服务端工具需要 Conversations 原生路由，OCR/Embedding 单独建立适配器。[Agents 工具](https://docs.mistral.ai/studio/agents/agent-tools) |

### 本次已经添加的具体参数代码

以下是已经进入生产源码的核心映射，省略了外围验证、路由与监控。完整实现以第 2 节文件为准：

```python
# Chat -> Gemini GenerateContent
generation_config["responseMimeType"] = "application/json"
generation_config["responseJsonSchema"] = response_format["json_schema"]["schema"]
generation_config["stopSequences"] = stop_sequences

# Chat -> Anthropic
anthropic_req.setdefault("output_config", {})["format"] = {
    "type": "json_schema", "schema": schema,
}
anthropic_tool["strict"] = openai_function["strict"]
anthropic_req["tool_choice"]["disable_parallel_tool_use"] = not parallel_tool_calls

# Gemini function call history: signature belongs beside functionCall
part = {"functionCall": function_call}
if tool_call.get("_thought_signature"):
    part["thoughtSignature"] = tool_call["_thought_signature"]
```

### 无需新增路由即可使用的配置方向

原生 Responses 内置工具的配置示例，模型 ID 和 Key 均是占位符，不会由该文档自动写入真实配置：

```json
{
  "my-responses-model": {
    "api_type": "responses_native",
    "api_base_url": "https://api.openai.com/v1",
    "endpoint_path": "/responses",
    "model_id": "UPSTREAM_MODEL_ID",
    "api_key": "UPSTREAM_API_KEY",
    "sanitize_recursive_schemas": false
  }
}
```

客户端向本网关 `/v1/responses` 发送 `tools: [{"type":"web_search"}]`。xAI 对应其官方 base URL 和工具类型。网关无需自己执行搜索；监控中仍应补充工具调用费用。是否对已有官方模型统一关闭 sanitizer，属于 D01。

百炼额外参数可放在已有 `extra_body_params`：

```json
{"enable_thinking": true, "thinking_budget": 8192}
```

这表示显式固定该模型配置的行为。需要按每个请求控制时，应直接在客户端请求体提供字段，避免在模型配置中覆盖它。

### 尚未添加路由的实施顺序与具体设计

1. **能力查询 / count_tokens**：先增加受认证的能力查询，只输出协议与已验证功能；再实现 Anthropic 原生 token 计数，复用消息转换但不要发送生成请求。模型原生 usage 与本地估算分别标注。
2. **Embeddings**：增加专用路由，认证/白名单复用现有入口，input 原样转发，单独解析 usage.prompt_tokens，不伪造 choices 和 output_tokens；统计写入要贯穿数据库查询和导出。
3. **Responses / Interactions 生命周期**：先建立下面的资源归属表，然后增加 GET/DELETE/cancel/input_items。选择上游 Key 后立即绑定，后续操作必须使用同一账号；不要根据客户端提供的 model 或 resource ID 重新轮询。
4. **Files / Batch / 显式缓存**：使用相同资源绑定模式，另加 multipart/二进制传输和大小限制。同步返回创建成功不等于后台任务完成。
5. **Audio / Images / OCR**：每种独立路由、错误映射和计费结构，支持二进制、URL、base64 的真实返回类型。
6. **Realtime / Live**：双向 WebSocket 代理，需要双向背压、关闭传播、会话限额、临时凭证与专门测试，不应包装成 SSE。

资源表建议（尚未执行数据库迁移）：

```sql
CREATE TABLE upstream_resources (
    gateway_id TEXT PRIMARY KEY,
    owner_key_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    upstream_id TEXT NOT NULL,
    endpoint_id TEXT NOT NULL,
    credential_id TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL
);
CREATE INDEX idx_resource_owner ON upstream_resources(owner_key_id, gateway_id);
```

后续读取路由的核心应当类似下面的模板。`resource_store`、`credential_store`、`endpoint_store`、`principal` 是待接入现有认证和配置 repository 的依赖，不是当前仓库已经存在的接口：

```python
from urllib.parse import quote
from fastapi import HTTPException

async def retrieve_response(gateway_id, principal, resource_store,
                            credential_store, endpoint_store, http_session):
    binding = await resource_store.find(gateway_id)
    if binding is None or binding.owner_key_id != principal.key_id:
        raise HTTPException(404, "Response not found")
    credential = await credential_store.resolve(binding.credential_id)
    endpoint = await endpoint_store.resolve(binding.endpoint_id)
    # base_url 来自保存的可信端点绑定，不能取客户端提交的任意 URL。
    url = endpoint.base_url.rstrip('/') + '/responses/' + quote(binding.upstream_id, safe='')
    async with http_session.get(url, headers={
        'Authorization': 'Bearer ' + credential.secret
    }) as upstream:
        payload = await upstream.json()
        return upstream.status, payload
```

还需补充资源 ID 对外映射、账号撤销/轮换行为、过期清理、跨重启恢复和审计。它们决定能否安全上线，不能用一个只转发 URL 的通配路由代替。

## 8. 新增实用功能与 LLM 工具

| 功能 | 价值 | 实施边界 |
|---|---|---|
| 请求 Playground | 在管理页验证协议、工具调用、结构化输出和错误表现 | 默认不自动发送；实际发送前展示目标模型与参数，不把模型 API Key 放到浏览器 |
| 能力检测与配置诊断 | 区分不支持 tools、格式错误、权限错误和不可达 | 静态诊断不调用上游；付费探测由管理员明确触发，保存最近验证时间 |
| 配置版本和差异恢复 | 避免批量修改后难以找回 | 版本文件含密钥，存储、访问与备份必须按凭证保护 |
| 请求取消 | 释放已经不需要的生成任务 | 取消信号必须贯穿上游连接和监控结束；不能只删除监控卡片 |
| 成本预算 / 限额 | 比单纯 RPM 更接近用户实际需求 | 需要调用方 ID、并发预算预留和最终结算；不要只在请求完成后才检查预算 |
| TTFT / 输出速率与错误趋势 | 帮助判断模型慢还是网关慢 | 按协议记录真实首 token，心跳和 reasoning 事件分别标注 |
| 安全导出诊断包 | 便于定位兼容错误 | 默认去除 Key、Authorization、对话全文、图片与签名；由用户选择是否附带敏感内容 |

已经提供一份可运行、带测试的只读工具原型：[`examples/llm_readonly_tools.py`](examples/llm_readonly_tools.py)。它没有注册到当前网关，不会自动开放新的权限。

- `bridge_list_models`：只列出调用方可见、未归档的模型。
- `bridge_describe_model`：返回模型协议和端点数量，不返回 Key、上游地址或完整配置，不声称静态协议配置已经通过运行检测。
- `bridge_usage_summary`：只允许管理员查询聚合统计。当前数据库不是按访客隔离，不能把这项聚合结果直接开放给访客。

调用示例：

```python
from docs.examples.llm_readonly_tools import ToolContext, TOOLS, execute_readonly_tool

context = ToolContext(
    models=model_snapshot,
    allowed_models=allowed_aliases,  # None=管理员完整集合；空集合=没有可见模型
    is_admin=authenticated_is_admin,
    usage_reader=read_usage_summary, # async(start_date, end_date) -> dict
)
result = await execute_readonly_tool(
    'bridge_describe_model', {'model': 'my-model'}, context,
)
```

接入时可以选择独立 MCP 服务或受认证的工具路由。建议先提供上述只读工具，再考虑配置修改、模型试调用、重试和归档工具。`run_shell`、任意 URL 抓取、任意文件访问、裸 SQL 不是这一阶段应开放的能力；若需要，应是独立沙箱服务和明确权限，而不是在网关进程里执行模型生成的代码。

## 9. 集中待确认项

主人可以一次回复下列选择，不需要逐条确认普通代码修改：

1. **默认兼容策略**：官方服务是否改为原样保留 strict/required/递归 schema，只对指定聚合商启用兼容清洗？建议是。
2. **重试策略**：是否采用“只在首个业务事件发送前重试”，区分短暂 429 与余额耗尽，并允许因此产生的重复上游请求？建议采用，并限制总尝试时间。
3. **部署定位**：这是单人网关，还是要提供访客自助监控/预算/资源管理的多人网关？这决定调用方 ID、资源表与数据库迁移。
4. **新增功能优先级**：建议先做能力诊断与 count_tokens，其次 Embeddings/资源生命周期，再做多模态专用路由。主人希望先落地哪一组？
5. **LLM 工具入口**：建议从独立 MCP 只读工具开始；若需要模型修改配置或调用上游，需要确认写权限和费用权限。
6. **安全与保留策略**：上传大小、日志保留、远程 tokenizer 代码、无 Key 图床部署、历史价格/汇率口径需要主人给出业务约束。

## 10. 最终验证记录

- 完整测试：`python -m pytest tests -q --disable-warnings --maxfail=3`，244 passed，1 warning，17.09 秒。起始基线为 167 passed，增加 77 个用例。最后一轮使用 `tests/conftest.py` 提供的独立临时运行目录。
- 语法检查：105 个 Python 文件 AST 解析、16 个非 vendor JavaScript 文件 `node --check`、2 段 HTML 内联脚本检查，全部通过；该检查还覆盖了未修改的旧脚本。随后新增的 conftest 也经过最终 pytest 实际加载执行。
- 新增浏览器回归运行真实 Chromium，验证模型编辑、JSONC 往返、旧日志响应竞态和监控标签切换。图床使用真实 FastAPI TestClient，持久化测试使用临时目录与数据库。
- 环境未安装 Ruff，因此没有声称执行 Ruff、完整类型检查或第三方依赖漏洞数据库扫描。
- 没有执行真实供应商调用、生产服务重启或真实数据迁移。需要这些验证时应另行制定脱敏 fixture 和调用预算。
- 修复提交：`ae3ddbd`、`dc9f1e8`。最终文档/测试隔离提交与远端推送状态以最终交付消息和仓库 Git 历史为准。

报告中的功能建议和路由模板不代表已经部署；第 2 节才是本轮实际修改。只读工具代码是未注册的样例模块，其权限测试已经包含在上述测试数中。
