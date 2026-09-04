# 面试备考：关键代码点串讲

> 配套文档：`project-walkthrough.md`（模块详解）。本文档按"面试官会盯着哪段代码问"组织。
> 每个点：位置 → 干什么 → 会怎么问 → 怎么答 → 追问预判。

---

## 1. Plan-Execute-Replan 状态图（`app/services/aiops_service.py`）

**干什么**：AIOps 诊断的主流程——固定拓扑三节点（Planner 定计划 → Executor 单步执行 → Replanner 评估决策），条件边控制循环或终止。

**会怎么问**："讲讲诊断流程怎么实现的？""为什么不用一个 ReAct Agent 直接跑？"

**怎么答**：
- 三节点固定拓扑 + 状态四字段（`input`/`plan`/`past_steps`/`response`），`response` 非空即终止
- 对话场景用 ReAct（`create_agent`）因为要灵活；诊断场景用固定拓扑因为**要可控、可审计、有步骤预算**——生产诊断不能让 Agent 自由发挥到失控

**追问预判**：
- "`past_steps` 为什么写 `Annotated[List[tuple], operator.add`]？" → LangGraph 默认状态更新是**覆盖**，加 reducer 变**追加**。⚠️ 这里可以主动讲你发现的 bug：追加语义导致初始状态传 `[]` 清不掉历史，同 session 重复诊断会状态污染，你加了 `adelete_thread` 修复——**这是最好的"我懂 reducer 语义"的证明**。
- "怎么流式给前端的？" → `stream_mode="updates"` 按节点吐增量 + 条件边路由。可以顺带讲你修的进度计数 bug（增量 vs 全量，`aget_state` 取全量）。

---

## 2. Replanner 防失控护栏（`app/agent/aiops/replanner.py:116-251`）

**干什么**：每执行一步后决策 continue/replan/respond。

**会怎么问**："怎么防止 Agent 死循环或乱来？"

**怎么答**（这是全项目最值得讲的设计）：
- **代码硬规则**（不靠 LLM 自觉）：已执行 ≥8 步强制出报告；≥5 步禁止 replan；replan 新步骤数按预算截断
- **Prompt 软引导**："任务完成守卫"——变更类任务"计划写过 ≠ 已完成"，未 commit 未验证禁止 respond
- **结构化输出**：`with_structured_output(Act)` 强制三选一，不许自由发挥

**追问预判**：
- "为什么软硬结合？" → LLM 是概率系统，**关键约束必须代码兜底**，prompt 只是引导。这是 Agent 工程的核心认知。
- "预算截断为什么是 `8 - 已执行` 而不是剩余步骤数？" → 代码注释里有答案：否则失败重规划时步骤越截越少直至丢失。说明你真的读过。

---

## 3. 上下文修剪中间件（`app/services/rag_agent_service.py:40-79`）

**干什么**：每次调 LLM 前修剪消息历史（保留系统消息 + 最近 3 轮）。

**会怎么问**："长对话上下文窗口爆了怎么办？"

**怎么答**：
- LangChain 1.3 的 `@before_model` 中间件协议，模型调用前介入
- 用 `RemoveMessage(REMOVE_ALL_MESSAGES)` + 重建列表实现修剪（LangGraph 官方推荐模式）
- checkpointer 里存全量，发给模型的才修剪——**存储与发送分离**

**追问预判**：
- ⚠️ 必防一问："修剪会把工具调用对切断吗？" → 诚实回答：当前按奇偶条数估算轮次，极端情况可能把 `AIMessage(tool_calls)` 和它的 `ToolMessage` 切开导致 API 报错，改进方向是工具调用对感知。**主动说出来是加分，被问出来才承认是减分**。
- 可讲的故事：这个中间件最初定义了但没接线（`create_agent` 没传 `middleware=`），你审计时发现并修复，还踩了泛型 `StateT` bound 到 LangChain 自带 `AgentState` 的类型坑。

---

## 4. MCP 重试拦截器（`app/agent/mcp_client.py:46-102`）

**干什么**：包在所有 MCP 工具调用外面的可靠性层。

**会怎么问**："外部工具/服务调用怎么保证可靠？"

**怎么答**：
- **拦截器模式**：重试逻辑与业务解耦，工具本身无感知
- **指数退避** 3 次（1s→2s→4s）
- 最关键的一笔：**全部重试失败后不抛异常，返回 `isError=True` 的 CallToolResult**——失败变成 LLM 能读懂的数据，Agent 循环永不因工具崩掉，LLM 自行决定换工具还是如实报告

**追问预判**：
- "为什么失败不直接抛异常？" → 抛出会中断整个 Agent 循环，用户面对一个崩溃；降级为数据让 Agent 自己处理，是"错误即数据"哲学。
- "MCP 是什么？" → Model Context Protocol，工具提供方和消费方的标准协议；本项目工具跑在独立进程（FastMCP，streamable-http），主服务通过 `MultiServerMCPClient` 接入，工具挂了主服务降级不崩。

---

## 5. Milvus 猴子补丁（`app/core/milvus_client.py:18-41`）

**干什么**：修 pymilvus 两套 API（ORM / MilvusClient）的连接别名不兼容。

**会怎么问**："遇到过第三方库的坑吗？怎么解决的？"

**怎么答**（答好了非常加分，证明你调试过真实的库冲突）：
- pymilvus 的 ORM 接口按别名管理连接；`langchain_milvus` 内部自建的 `MilvusClient` 别名是 `cm-{随机id}`，没注册进 ORM 的连接表
- 后续 ORM 的 `Collection(using=...)` 找不到别名，抛 `ConnectionNotExistException`
- 修法：劫持 `MilvusClient.__init__`，构造后强制 `self._using = "default"`，两套 API 共用一条连接；`_done` 属性保证只补丁一次

**追问预判**：
- "猴子补丁不怕升级挂掉吗？" → 怕，所以补丁逻辑里有 `ImportError` 兜底；长期方案是关注上游修复，补丁是权宜之计。这个回答展示风险意识。

---

## 6. RAG 写入链（`document_splitter_service.py` + `vector_index_service.py`）

**干什么**：文档 → 切块 → 向量化 → Milvus。

**会怎么问**："RAG 怎么保证检索质量？""chunk 怎么切的？"

**怎么答**：
- 三阶段切分：**Markdown 标题感知**（按 `#`/`##` 语义章节切，不按字数硬切）→ 递归字符二次切（超长章节）→ **小碎片合并**（<300 字符合并，避免无意义小向量）
- 切块带章节链 metadata（h1>h2>h3），检索结果格式化时还原出处——LLM 知道每段话出自哪个章节
- 重传同名文件 = 先删旧（`metadata["_source"]` 匹配删除）再插入，保证知识库不腐化

**追问预判**（数字必须背熟，别被问倒）：
- chunk 多大？ → 配置 `chunk_max_size=800`，但二次切分实际用 **×2=1600**，overlap 100（为减少分片数）
- 向量维度/模型？ → 1024 维，`text-embedding-v4`
- 索引类型？ → 服务端 IVF_FLAT（nlist=128），Lite 模式 FLAT 暴力扫；距离度量 L2
- ⚠️ 防一问："换 embedding 模型会怎样？" → 维度不匹配时 `connect()` 会**删库重建**——这是你知道并接受的设计取舍（维度不对检索必失败，不如重建）。

---

## 7. SSE 流式（`app/api/chat.py` + `static/app.js`）

**干什么**：打字机效果的实时输出。

**会怎么问**："流式怎么实现的？为什么不用 WebSocket？"

**怎么答**：
- 后端：`sse_starlette` 的 `EventSourceResponse` + 内部异步生成器，把服务层 chunk 翻译成前端事件格式
- 前端：`fetch` + `ReadableStream` 手动解帧（按 `\n` 切行、`data:` 行 JSON.parse）
- **为什么 SSE 不用 WebSocket**：单向推送场景 SSE 足够且更简单——基于普通 HTTP、过代理友好、无需协议升级；WebSocket 的双向能力这里用不上
- **为什么前端不用原生 EventSource**：它只支持 GET，我们接口是 POST（带 JSON body）

**追问预判**：
- "出错了怎么办？" → 异常不中断连接，转为 `error` 事件发出，前端正常收尾。
- "SSE 的 data 能放 JSON 对象吗？" → 不能，协议只认文本，所以要 `json.dumps(ensure_ascii=False)`，前端再 parse。

---

## 8. 会话持久化（MemorySaver + thread_id）

**会怎么问**："多轮对话怎么实现的？"

**怎么答**：
- LangGraph checkpointer 机制：`thread_id = session_id`，每轮状态自动存档
- 内存版 `MemorySaver`，重启丢失；换 `SqliteSaver` 等即可持久化，接口不变（checkpointer 是可插拔抽象）
- 历史查询是翻 checkpoint 的 `channel_values.messages`

**主动坦白**（加分项）：历史接口的时间戳是假的（LangChain 消息对象没有 timestamp 属性，代码退化成了查询时刻的 `datetime.now()`），已列入改进清单。

---

## 9. 防火墙评测沙盒（`mcp_servers/firewall_server.py`）

**会怎么问**："这个防火墙是干什么的？"（面试官大概率没见过这种东西，会好奇）

**怎么答**（这是差异化武器，讲出彩）：
- 它不是业务功能，是**Agent 能力评测环境**：模拟真实网络设备的两阶段提交语义（candidate/running 配置、commit 生效、discard 回滚）
- **故障注入**是核心：`commit_flaky` 测 Agent 的重试策略；`commit_lose` 最精妙——**实际生效但返回超时失败**，测 Agent 面对"结果歧义"会不会用只读接口（overview/diff）核实真实状态，而不是盲目重试或谎报失败
- `/admin/*` 管理通道（reset/scenario/snapshot）不暴露给 Agent，供评测脚本注入故障、导出快照打分——评测方与被评测方通道分离

**追问预判**：
- "为什么需要这个？" → Agent 的能力不能只看 demo 跑通，要在"设备会抖动、会撒谎"的环境里压测。这体现你对 Agent 工程成熟度的理解远超"调 API 套壳"。

---

## 10. 全局降级哲学（贯穿全项目）

**会怎么问**："系统可用性怎么设计的？""你这个项目工程上最大的亮点是什么？"

**怎么答**（一条完整的链，层层递进）：
1. Milvus 挂了 → 知识库降级，对话/诊断照常（lifespan try/except）
2. MCP 服务挂了 → 只用本地工具，Agent 照常（`load_mcp_tools_safe`）
3. 工具调用失败 → 返回错误字符串/`isError` 结果，Agent 循环不中断
4. LLM structured output 抖动返回 None → 重试一次，再不行用兜底计划/兜底报告
5. 每层都"失败降级不崩溃"，且降级后的信息都以 LLM 能读懂的形式呈现

---

## 附：三条必练的"大题"

1. **五分钟画架构图**：浏览器 → FastAPI（薄路由）→ 双引擎（ReAct 对话 / Plan-Execute-Replan 诊断）→ 工具层（本地 3 件 + MCP 3 服务）→ 数据层（Milvus / DashScope / Prometheus）。考前默画两遍。

2. **"代码是 AI 写的吗？"** —— 诚实 + 反转叙事："AI 辅助搭建，然后我做了完整深度审计：清理了 3 处死代码、修复了 5 个真实 bug（状态污染、进度计数、中间件未接线……），统一了前后端契约。"每个修复都有文档记录（`project-walkthrough.md`），展示的是**代码审查能力**。

3. **"有什么改进计划？"** —— 照这个清单挑三条说：持久化 checkpointer（SqliteSaver）、修剪中间件升级为工具调用对感知、Milvus 健康检查改为真 ping、aiops 节点级 LLM/MCP 客户端复用、上传索引进后台任务（BackgroundTasks）。

---

## 附：必须背熟的数字

| 数字 | 含义 | 位置 |
|---|---|---|
| 1024 | 向量维度（text-embedding-v4） | milvus_client.py:49 |
| 1600 / 100 | 实际 chunk 大小（800×2）/ overlap | document_splitter_service.py:33 |
| 3 | RAG top_k | config.py:42 |
| 8 / 5 | replanner 步数硬顶 / 禁 replan 阈值 | replanner.py:135,218 |
| 3 次（1s→2s→4s） | MCP 重试 + 指数退避 | mcp_client.py:49 |
| 8003/8004/8005/9900 | CLS/Monitor/Firewall/主服务端口 | config.py:52-56 |
| 10MB | 上传大小上限（前后端已对齐） | file.py:18, app.js:1119 |
| 7 天 | 日志保留期 | logger.py:36 |
