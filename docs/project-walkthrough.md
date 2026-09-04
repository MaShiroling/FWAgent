# FireDrill 项目模块详解

> 本文档按模块逐一详解项目实现细节，持续更新。

## 模块地图

1. **应用入口与基础设施** — `main.py` / `config.py` / `logger.py`
2. **API 路由层** — `app/api/`（chat、aiops、file、health 四个接口）
3. **数据模型层** — `app/models/`（请求/响应的 Pydantic 模型）
4. **核心组件** — `app/core/`（LLM 工厂、Milvus 客户端）
5. **RAG 服务链** — `app/services/` 的向量存储五件套（embedding、切分、索引、检索、存储管理）
6. **RAG Agent 服务** — `rag_agent_service.py`（LangGraph 状态图对话）
7. **AIOps 诊断引擎** — `app/agent/aiops/`（Plan-Execute-Replan 三件套 + 状态机）
8. **MCP 集成** — `app/agent/mcp_client.py` + `mcp_servers/` 三个服务
9. **工具集与前端** — `app/tools/` + `static/`

---

# 模块一：应用入口与基础设施

涉及文件：`app/main.py`、`app/config.py`、`app/__init__.py`、`app/utils/logger.py`。这四个文件是整个应用的"地基"——任何一个请求进来，最先执行的就是它们。

## 1. 启动链条：从 `uvicorn` 到服务就绪

通过 `python -m uvicorn app.main:app --host 0.0.0.0 --port 9900` 启动服务，Python 的 import 机制会按这个顺序执行：

```
导入 app.main
  → 先触发 app/__init__.py（包初始化）
      → import app.utils.logger → 执行 setup_logger()，日志系统就绪
  → 触发 app/config.py → 创建全局 config 单例，读取 .env
  → 执行 main.py 顶层代码 → 创建 FastAPI 实例、挂中间件、注册路由
  → uvicorn 调用 lifespan() → 连接 Milvus → 开始接收请求
```

### `app/__init__.py`（app/__init__.py:8）

只有一行关键代码 `from app.utils import logger`，但它很重要：这保证**只要 import 了 app 包下的任何模块，日志配置就一定已经执行**。这是一种"导入即配置"的惯用法，避免了在 main.py 里手动调初始化函数的遗漏风险。

## 2. `config.py`：配置管理

典型的 **Pydantic Settings** 模式（app/config.py:10-87），核心设计：

- **`Settings` 继承 `BaseSettings`**：字段声明即配置项。Pydantic 会自动按字段名（大小写不敏感）去 `.env` 文件和环境变量里找值，找不到就用默认值。比如 `dashscope_api_key` 对应 `.env` 里的 `DASHSCOPE_API_KEY`。
- **`extra="ignore"`**（config.py:17）：`.env` 里多出来的键不会报错，方便塞一些临时变量。
- **配置分组**很清晰：
  - 应用自身：`host`/`port`/`debug`，默认端口 9900
  - DashScope：`dashscope_model = "qwen-max"`，embedding 用 `text-embedding-v4`
  - Milvus：支持两种模式——`milvus_host:port` 连 Docker 里的 Milvus 服务，或设 `milvus_lite_path` 走**嵌入式 Milvus Lite**（免 Docker，数据存本地 `.db` 文件，对应目录里的 `volumes/milvus_lite.db/`）
  - RAG 参数：`rag_top_k=3`（检索返回 3 条）、`chunk_max_size=800` / `chunk_overlap=100`（文档切块大小与重叠）
  - MCP 服务：三个服务器的 transport 和 URL（cls 日志 8003、monitor 监控 8004、firewall 防火墙 8005）

两个 **`@property`** 值得注意：

- `milvus_lite_mode`（config.py:62-65）：把"是否启用 Lite 模式"这个判断封装成属性，业务代码不用每次写 `if config.milvus_lite_path != ""`。
- `mcp_servers`（config.py:67-83）：把三个 MCP 服务的扁平配置聚合成一个嵌套字典，这正是后面 `mcp_client.py` 里 `MultiServerMCPClient` 需要的入参格式——**配置层提前适配了消费方的数据结构**，是个好习惯。

文件末尾 `config = Settings()`（config.py:87）创建**模块级单例**，全项目统一 `from app.config import config`，保证配置只解析一次。

## 3. `logger.py`：日志系统

基于 **Loguru**（比标准库 logging 好用得多），`setup_logger()` 做了三件事（app/utils/logger.py:19-44）：

- `logger.remove()`：先删掉 Loguru 默认的 stderr 输出，避免重复打印。
- **控制台输出**：彩色格式，包含时间、级别、模块.函数：行号。`diagnose=config.debug` 是个细节——生产模式不打印变量值，避免敏感信息进日志。
- **文件输出**：写到 `logs/app_{date}.log`，`rotation="00:00"` 每天零点切分，`retention="7 days"` 只留 7 天，过期自动 zip 压缩（这解释了 logs 目录里为什么会有 `app_2026-08-28.log.zip`）。`enqueue=True` 表示异步写入——日志先进队列，由后台线程落盘，**不会阻塞请求处理**，这对 Web 服务性能很重要。

## 4. `main.py`：FastAPI 应用组装

### lifespan 生命周期（main.py:19-44）

用的是 FastAPI 现代的 `@asynccontextmanager` 写法（而不是旧的 `on_event`）。`yield` 之前是启动逻辑，之后是关闭逻辑：

- 启动时调用 `milvus_manager.connect()` 连接向量库。**关键设计：用 try/except 包住，失败只降级不崩溃**——Milvus 连不上时，RAG 知识库功能不可用，但普通对话和 AIOps 诊断照常工作。这体现了"向量库是可选依赖"的容错思路。
- 关闭时 `milvus_manager.close()` 优雅释放连接。

### 应用组装（main.py:48-84）

- **CORS 中间件**：`allow_origins=["*"]` 全放开，开发方便，但代码注释里也自知"生产环境应该限制具体域名"。
- **路由注册**：四个 router 对应四大功能——`health`（健康检查，无前缀）、`chat` / `file` / `aiops`（都挂 `/api` 前缀）。
- **静态文件**：`/static` 路径挂载前端三件套，`GET /` 直接返回 `index.html`，所以访问 `http://localhost:9900` 就能看到 Web 界面——**前后端是同一个 FastAPI 进程托管的**，没有单独的 Nginx。

### 一个值得知道的小细节

`if __name__ == "__main__"` 里的 `uvicorn.run`（main.py:87-96）其实**不会被执行**——因为 README 里是用 `python -m uvicorn app.main:app` 启动的。这段代码只是给直接 `python app/main.py` 的方式留的后门，且带了 `reload=config.debug` 的热重载。

## 本层小结

这一层没有业务逻辑，职责是"配置集中管理 + 日志全局就绪 + 组装 FastAPI + 容错连接 Milvus"。理解它的价值在于：后续所有模块拿配置都是 `from app.config import config`，写日志都是 `from loguru import logger`，这两个单例是全项目的公共契约。

---

# 模块二：API 路由层（`app/api/`）

涉及文件：`chat.py`、`aiops.py`、`file.py`、`health.py`。这一层是整个系统的"门面"——所有外部请求（前端、curl、其他系统）都从这里进入。

## 1. 这一层的定位：纯控制器，零业务逻辑

先建立一个全局认识——**API 层只做三件事：校验入参、调用服务层、包装响应**。真正的智能逻辑全在下一层（services/agent）：

```
┌─────────────┐    HTTP      ┌──────────────────────────────────┐
│  前端/curl   │ ───────────→ │  FastAPI 路由层 (app/api/)        │
└─────────────┘              │                                   │
                             │  ┌───────────┐  ┌──────────────┐  │
                             │  │ chat.py   │  │ aiops.py     │  │
                             │  │ /chat     │  │ /aiops       │  │
                             │  │ /chat_…   │  └──────┬───────┘  │
                             │  └────┬──────┘         │          │
                             │  ┌────┴──────┐  ┌──────┴───────┐  │
                             │  │ file.py   │  │ health.py    │  │
                             │  │ /upload   │  │ /health      │  │
                             │  └────┬──────┘  └──────┬───────┘  │
                             └───────┼─────────────────┼──────────┘
                                     ↓                 ↓
                          ┌─────────────────────────────────────┐
                          │  服务层 (app/services/)              │
                          │  rag_agent_service / aiops_service   │
                          │  vector_index_service                │
                          └─────────────────────────────────────┘
```

对照 `main.py` 里的注册代码（main.py:65-68），四个 router 的挂载情况和全部接口如下：

| 文件 | 挂载前缀 | 接口 | 方法 | 职责 |
|------|---------|------|------|------|
| `health.py` | 无 | `/health` | GET | 服务 + Milvus 健康检查 |
| `chat.py` | `/api` | `/api/chat` | POST | 一次性对话 |
| | | `/api/chat_stream` | POST | SSE 流式对话 |
| | | `/api/chat/clear` | POST | 清空会话 |
| | | `/api/chat/session/{id}` | GET | 查询会话历史 |
| `file.py` | `/api` | `/api/upload` | POST | 上传文档并建索引 |
| | | `/api/index_directory` | POST | 批量索引目录 |
| `aiops.py` | `/api` | `/api/aiops` | POST | SSE 流式故障诊断 |

注意 `health.router` 没加 `prefix="/api"`，所以它的路径是 `/health` 而不是 `/api/health`——README 里写的 `/api/health` 和实际代码对不上，以代码为准。

## 2. `chat.py`：对话接口

三个对话相关接口，核心区别在于**响应方式**。

### 2.1 `/api/chat`：一次性返回（chat.py:18-66）

流程非常简单：

```
POST /api/chat {"Id":"session-123","Question":"你好"}
        │
        ▼
Pydantic 校验 ChatRequest（字段 Id/Question，模型层细说）
        │
        ▼
rag_agent_service.query(question, session_id)   ← 阻塞等待完整答案
        │
        ▼
返回 {"code":200, "data":{"success":true, "answer":"...", "errorMessage":null}}
```

两个细节：

- **异常被吞进响应体**：出错时不抛 HTTP 500，而是返回 `code: 500` 的 JSON（chat.py:56-66）。HTTP 状态码永远是 200，前端要靠 `code` 字段判断成败——这是仿 Java 后端的"统一响应包"风格，`errorMessage` 用驼峰命名也印证了这点（大概率前端或上游系统是 Java 技术栈）。
- 整个请求是**阻塞式**的：LLM 生成完整答案才返回，用户等待期间看不到任何中间过程。这就是"快速问答"模式。

### 2.2 `/api/chat_stream`：SSE 流式输出（chat.py:69-170）

这是前端体验的关键接口，用 `sse_starlette` 的 `EventSourceResponse` 实现 **SSE（Server-Sent Events）**——一种基于 HTTP 长连接的单向推送协议。

```
客户端                      FastAPI                      RAG Agent 服务
  │  POST /chat_stream        │                               │
  │──────────────────────────→│                               │
  │                           │── query_stream() 异步生成器 ──→│
  │                           │                               │── 检索知识库
  │  ◄── {type:search_results}│◄──────────────────────────────│
  │  ◄── {type:tool_call}     │◄── 调用工具（如查知识库）       │
  │  ◄── {type:content} "你"  │◄── LLM 逐 token 生成 ─────────│
  │  ◄── {type:content} "好"  │◄──────────────────────────────│
  │  ◄── {type:content} "..." │◄──────────────────────────────│
  │  ◄── {type:done}          │◄── 生成完毕 ──────────────────│
  │     （连接关闭）            │                               │
```

代码结构是一个**内部异步生成器** `event_generator()`（chat.py:95），它从 `rag_agent_service.query_stream()` 拿到服务层吐出的 chunk，然后按 `type` 翻译成前端约定的事件格式。服务层与前端之间的**事件名映射**是这段代码的核心职责：

| 服务层 chunk type | 发给前端的 type | 含义 |
|---|---|---|
| `debug` | `debug` | 节点调试信息（哪个 LangGraph 节点在跑） |
| `tool_call` | `tool_call` | 工具调用开始/结束，前端可显示"正在查询知识库…" |
| `search_results` | `search_results` | 检索到的文档片段 |
| `content` | `content` | LLM 输出的内容块（打字机效果的原料） |
| `complete` | `done` | 完整答案 + 工具调用记录汇总 |
| `error` | `error` | 错误信息 |

每个事件都是 `event: message` + `data: <JSON 字符串>` 的 SSE 帧。注意 chat.py:131 的注释"关键：data 必须是 JSON 字符串"——SSE 协议的 `data` 字段只能是文本，所以中文内容要 `json.dumps(..., ensure_ascii=False)` 序列化，前端再 `JSON.parse` 还原。

异常处理（chat.py:160-168）：生成器内部捕获异常后**不中断连接**，而是发一个 `type: error` 事件再结束，前端收到后能正常收尾，而不是面对一个突然断掉的流。

### 2.3 会话管理两个接口

- `POST /api/chat/clear`（chat.py:173-195）：调 `rag_agent_service.clear_session()` 清掉内存里的会话历史。
- `GET /api/chat/session/{session_id}`（chat.py:198-219）：返回该会话的历史消息列表和条数。

这两个接口说明**会话历史是存在服务层内存里的**（具体实现见 `rag_agent_service` 模块），重启即丢失。

## 3. `aiops.py`：故障诊断接口

整个文件只有一个接口 `POST /api/aiops`（aiops.py:16-153），也是 SSE 流式，但事件结构完全不同——它推送的是**诊断流程的阶段进度**：

```
POST /api/aiops {"session_id":"xxx"}
   │
   ▼
aiops_service.diagnose() 异步生成器按阶段吐事件：
   │
   ├─→ status        "正在获取系统告警信息..."        (fetching_alerts)
   ├─→ plan          "诊断计划已制定，共 6 个步骤"      (plan_created)
   ├─→ step_complete "步骤执行完成 (2/6)"             (step_executed)  × N 次
   ├─→ report        "最终诊断报告已生成"              (final_report)
   └─→ complete      "诊断流程完成"                   (diagnosis_complete)
                                                      │
              ┌─── 收到 complete/error 后 break ──────┘
              ▼
         关闭 SSE 连接
```

和 `chat_stream` 的区别在于：这里**不做事件翻译**，服务层吐什么就直接 `json.dumps` 转发（aiops.py:129-134），因为事件格式本身就是 aiops_service 按前端契约设计好的。`event.get("type") in ["complete", "error"]` 时主动 `break` 结束生成器（aiops.py:137-138），让连接干净关闭。

~~⚠️ 一个**文档里的坑**：docstring（aiops.py:98-116）给的前端示例用了 `new EventSource('/api/aiops')`，但浏览器原生 `EventSource` **只支持 GET 请求**，而这个接口是 POST——那段示例代码实际跑不起来。~~（已修复 ✅：aiops.py docstring 已按真实事件集重写，删除了跑不通的 EventSource 示例和虚构的 `fetching_alerts`/`target_alert`/`evidence` 字段，并注明前端应使用 fetch + ReadableStream。）

## 4. `file.py`：文件上传与索引

### 4.1 `POST /api/upload`（file.py:21-97）

这是知识库文档的入口，处理链路：

```
上传文件
   │
   ▼
① 文件名校验（非空）
   ▼
② _sanitize_filename()  ── 空格和 \ / : * ? " < > | 替换为下划线
   ▼                       （防路径穿越，如 "../../etc/passwd"）
③ 扩展名白名单校验（仅 txt/md）
   ▼
④ 保存到 ./uploads/（同名文件先删旧→实现"覆盖更新"语义）
   ▼
⑤ 大小校验 ≤ 10MB
   ▼
⑥ vector_index_service.index_single_file()  ← 同步建向量索引
   │  ⚠ 索引失败只记日志，不影响上传成功
   ▼
返回 200 + 文件信息
```

两个设计决策值得注意：

- **索引失败 ≠ 上传失败**（file.py:75-77）：文件落盘成功就返回 200，索引失败只是记 error 日志。好处是文件不会丢，坏处是用户可能以为知识库已更新但实际没索引上。
- **索引是同步阻塞的**（file.py:73）：`index_single_file` 不是 async，会在事件循环里阻塞——文件大、切块多的时候，这个请求会卡住整个进程的其他请求一小段时间。生产环境更合理的做法是放进后台任务（`BackgroundTasks`）异步建索引。

### 4.2 `POST /api/index_directory`（file.py:100-128）

批量索引整个目录，默认索引 `./uploads`。返回的 `result.to_dict()` 里会带成功/失败统计（见 `vector_index_service` 模块的结果模型）。

这里有个**类型标注瑕疵**：`directory_path: str = None`（file.py:101）在严格类型检查下应该是 `Optional[str]`，项目把 pyright 的 `reportOptionalMemberAccess` 关了所以没报错。

## 5. `health.py`：健康检查

`GET /health`（health.py:13-64）逻辑直白：查一次 `milvus_manager.health_check()`，Milvus 通就 200，不通就 **503 + `status: unhealthy`**。

有意思的是它和 `main.py` 的**哲学不一致**：启动时 Milvus 连不上是"降级可用"（只警告不崩溃），但健康检查却把它判为 503"服务不可用"。如果前面挂负载均衡，Milvus 抖动会导致整个实例被摘流量，普通对话功能其实还活着却被连累下线。这两个策略谁对没有标准答案，但要知道这个矛盾存在。

## 本层小结

- 四个 router = 四组业务能力，路由薄、服务厚，分层干净。
- 两种响应范式：**一次性 JSON**（chat、upload、health）和 **SSE 流**（chat_stream、aiops），流式接口都靠内部 `event_generator()` 异步生成器 + `EventSourceResponse`。
- 错误处理三种风格并存：吞进响应体（chat）、发 error 事件（两个流式接口）、`HTTPException`（file/health 的部分路径）——不统一，但各自适配了前端的不同消费方式。
- 下一层的三个关键入口已经露面：`rag_agent_service`、`aiops_service`、`vector_index_service`。

---

# 模块三：数据模型层（`app/models/`）

涉及文件：`request.py`、`response.py`、`aiops.py`。这一层是系统的**数据契约**——定义了"前端发给后端的数据长什么样、后端返回的数据长什么样"。

> 注：本层曾存在 `document.py`（`DocumentChunk` 模型）和若干未使用的响应模型，已在一次对齐修复中清理，详见第 6 节"历史问题与修复记录"。

## 1. 定位与全貌

```
┌──────────┐  HTTP JSON  ┌─────────────┐  Python 对象  ┌──────────┐
│  前端     │ ──────────→ │ 请求模型      │ ───────────→ │  服务层   │
│          │             │ ChatRequest  │              │          │
│          │             │ AIOpsRequest │              │          │
│          │ ←────────── │ 响应模型      │ ←─────────── │          │
└──────────┘             │ ChatResponse │              └──────────┘
                         └─────────────┘
                  Pydantic 负责：JSON ↔ 对象的双向转换 + 校验
```

当前全部 7 个模型及其用途（均在实际使用）：

| 模型 | 文件 | 用途 |
|------|------|------|
| `ChatRequest` | request.py | `/api/chat`、`/api/chat_stream` 入参 |
| `ClearRequest` | request.py | `/api/chat/clear` 入参 |
| `AIOpsRequest` | aiops.py | `/api/aiops` 入参 |
| `ChatResponse`（含 `ChatData`） | response.py | `/api/chat` 响应（`response_model` 约束） |
| `ApiResponse` | response.py | `/api/chat/clear` 响应 |
| `SessionInfoResponse` | response.py | `/api/chat/session/{id}` 响应 |
| `HealthResponse` | response.py | `/health` 响应（`response_model` 约束） |

## 2. `request.py`：alias 机制是重点

### `ChatRequest`（request.py:9-22）

```python
class ChatRequest(BaseModel):
    id: str = Field(..., description="会话 ID", alias="Id")
    question: str = Field(..., description="用户问题", alias="Question")

    class Config:
        populate_by_name = True
```

三个知识点：

- **`Field(...)` 里的 `...`**：是 Python 的 `Ellipsis` 对象，在 Pydantic 里表示"必填"。
- **`alias="Id"`**：这是理解整个项目 API 风格的钥匙。Python 字段名是 `id`（蛇形小写），但对外接受的 JSON 键是 **`Id`**（大写帕斯卡）——这是 C#/Java 的命名习惯。所以前端要发 `{"Id": "...", "Question": "..."}`，而后端代码里写的是 `request.id`。Pydantic 在中间自动做映射。**这印证了对面的前端/上游系统是 Java 或 C# 技术栈**，本项目在迁就对方的契约。
- **`populate_by_name = True`**：允许**同时**用别名（`Id`）和字段名（`id`）两种键传参，兼容性更好。

### 命名风格不一致的证据

对比 `ClearRequest`（request.py:25-31）：它的别名是 `sessionId`（**小写驼峰**），而 `ChatRequest` 用的是 `Id`/`Question`（**大写帕斯卡**）。两个请求模型用了两套命名约定——大概率是不同轮次 AI 生成、或对接了不同上游接口留下的痕迹。`AIOpsRequest` 干脆没用别名，直接 `session_id`。

## 3. `response.py`：响应契约与实现已对齐

### `ChatResponse` + `ChatData`（response.py:10-25）

```python
class ChatData(BaseModel):
    success: bool
    answer: Optional[str] = None
    error_message: Optional[str] = Field(None, alias="errorMessage")

    model_config = ConfigDict(populate_by_name=True)

class ChatResponse(BaseModel):
    code: int
    message: str
    data: ChatData
```

这套结构精确描述了 `/api/chat` 的真实返回 `{"code":200,"message":"success","data":{"success":true,"answer":"...","errorMessage":null}}`。接口通过 `@router.post("/chat", response_model=ChatResponse)`（chat.py:18）挂上模型：路由函数仍然返回手拼的 dict，FastAPI 会拿模型校验并过滤它；序列化时默认 `by_alias=True`，所以 `errorMessage` 按驼峰正确输出。收益是 `/docs` 自动生成响应文档，且返回结构有了校验兜底——形状写错会直接报 500 而不是悄悄把脏数据发给前端。

### `HealthResponse`（response.py:44-49）

`code/message/data` 三段式，`data` 是动态明细 dict（服务信息 + Milvus 状态）。`health.py` 改为注入 `Response` 对象控制状态码：

```python
@router.get("/health", response_model=HealthResponse)
async def health_check(response: Response):
    ...
    response.status_code = status_code  # 200 或 503
    return HealthResponse(code=status_code, message=..., data=health_data)
```

### 其余两个在用模型

- **`ApiResponse`**（response.py:36-41）：`status/message/data` 三段式通用响应，被 `/api/chat/clear` 使用。
- **`SessionInfoResponse`**（response.py:28-33）：`session_id` + `message_count` + `history`（`List[Dict[str, str]]`，即 `[{"role": "...", "content": "..."}]` 形态的消息列表），被会话查询接口使用。

## 4. `aiops.py`：只保留请求模型

该文件现在只有 **`AIOpsRequest`**（aiops.py:9-22）：一个可选字段 `session_id`，默认值 `"default"`，不传也能调。诊断结果走 SSE 事件流推送，没有一次性 JSON 响应，因此这个文件里没有、也不需要响应模型。

## 5. 本层小结

- 模型层的价值：**入参校验**（Pydantic 自动 422）和**契约文档**（`/docs` 自动生成）。
- `alias` + `populate_by_name` 是对接异构前端的关键机制，`Id`/`Question` 的帕斯卡命名暴露了对端是 Java/C# 栈。
- 请求模型对外接受别名、对内暴露蛇形字段；响应模型通过 `response_model` 约束出口，双向都有保障。
- 两个流式接口（`/api/chat_stream`、`/api/aiops`）不走响应模型——SSE 事件流由生成器逐帧产出，无法用 `response_model` 描述，它们的契约写在各自的 docstring 里。

## 6. 历史问题与修复记录

**修复前的问题**：本层曾定义 10 个模型，其中 5 个是从未被 import 的死代码——`ChatResponse`（当时定义的 `answer/session_id` 字段和 `/api/chat` 实际返回完全对不上）、`HealthResponse`（同理，health.py 手拼 dict）、`AlertInfo`（aiops 服务内部用 dict 传告警）、`DiagnosisResponse`（为一个从不存在的"非流式诊断接口"预留）、`DocumentChunk`（切分服务实际用 LangChain 的 `Document`）。典型的 AI 生成痕迹：模型定义追求"完整"，但业务代码只消费了一半，导致**对外契约实际由路由里的字面量决定，模型文件会误导读代码的人**。

**修复原则**：模型必须描述真实返回的 JSON，而不是改变接口去迁就模型——前端已在按现有格式消费，改返回结构会把前端搞挂。

**具体动作**：

- 重写 `ChatResponse` 为真实的 `code/message/data` 三段式（新增 `ChatData` 子模型，`errorMessage` 走 alias），挂到 `/api/chat` 的 `response_model` 上（chat.py:18）。
- 重写 `HealthResponse` 为 `code/message/data` 结构；`health.py` 改用注入 `Response` 对象设置 200/503 + `response_model=HealthResponse`，不再返回 `JSONResponse`。
- 删除 `AlertInfo`、`DiagnosisResponse`，`app/models/document.py` 整文件删除。

**验证**：真实返回 dict 与新模型双向校验通过（含 `errorMessage` 别名往返一致）；`/openapi.json` 中 `/api/chat` 和 `/health` 正确引用 `ChatResponse`/`HealthResponse`；线上返回 JSON 一字节未变，前端无感知。

---

# 模块四：核心组件（`app/core/`）

涉及文件：`milvus_client.py`（当前唯一文件）。这一层的定位是**外部资源的访问封装**。

> 注：本层曾存在 `llm_factory.py`（基于 OpenAI 兼容协议的 LLM 工厂），因从未被任何代码使用已删除，详见第 3 节"已删除的 llm_factory"。

## 1. 本层定位

```
┌──────────────────────────────────────────────────────┐
│  服务层 (rag_agent_service / aiops / vector_*)        │
└───────┬──────────────────────────────┬───────────────┘
        │ LLM 调用                      │ 向量存取
        ▼                              ▼
┌───────────────────┐          ┌───────────────────────┐
│ 各服务直接创建      │          │ milvus_client.py      │
│ ChatQwen           │          │ MilvusClientManager   │
│ (langchain_qwq)    │          │ （全局单例）            │
└───────────────────┘          └───────────┬───────────┘
                                           │
                                ┌──────────┴──────────┐
                                │ Milvus Server(Docker)│
                                │ 或 Milvus Lite(本地)  │
                                └─────────────────────┘
```

注意一个现状：**LLM 的创建并没有走 core 层统一封装**，各服务（rag_agent_service、aiops 的 planner/executor/replanner）各自 `new ChatQwen(...)`。core 层实际只剩 Milvus 这一个资源的封装。

## 2. `milvus_client.py`：真正的核心（全项目都靠它）

### 2.1 职责与单例

`MilvusClientManager`（milvus_client.py:44）管理 Milvus 连接的完整生命周期，文件末尾 `milvus_manager = MilvusClientManager()`（milvus_client.py:341）创建**全局单例**，被 `main.py`（启动连接）、`health.py`（健康检查）、`vector_store_manager.py`（向量读写）三处共享。

### 2.2 `connect()` 的完整流程（milvus_client.py:59-150）

```
connect()
   │
   ▼
① 幂等检查：已连接就直接返回（因为 import 阶段可能已被提前连接过）
   ▼
② _patch_pymilvus_milvus_client_orm_alias()   ← 见 2.3，关键补丁
   ▼
③ 分支：Lite 模式 or 服务模式
   ├─ Lite: connections.connect(uri="./volumes/milvus_lite.db")  ← 嵌入式，免 Docker
   └─ 服务: connections.connect(host:port) + MilvusClient(http://host:port)
   ▼
④ collection 'biz' 存在？
   ├─ 不存在 → 创建（见 2.4 schema）
   └─ 已存在 → 检查 vector 维度！
        ├─ 维度 ≠ 1024 → ⚠ 删库重建（drop_collection + 重新创建）
        └─ 维度 = 1024 → 通过
   ▼
⑤ _load_collection()：加载到内存（不加载无法检索）
```

第 ④ 步有个**必须知道的危险设计**：维度不匹配时直接 `drop_collection` 重建（milvus_client.py:122-130）。逻辑上合理（维度不对检索必然失败），但**意味着换 embedding 模型后重启服务，旧知识库数据会被静默清空**。`VECTOR_DIM=1024`（milvus_client.py:49）对应配置里 `text-embedding-v4` 的输出维度。

### 2.3 猴子补丁：`_patch_pymilvus_milvus_client_orm_alias()`（milvus_client.py:18-41）

全项目最"硬核"的一段代码，修的是一个**第三方库之间的兼容 bug**：

- `pymilvus` 有两套 API：老式 ORM 风格（`Collection`、`connections.connect`，按别名管理连接）和新式 `MilvusClient`。
- `langchain_milvus`（LangChain 的 Milvus 集成，RAG 服务链要用它）内部会自己 new 一个 `MilvusClient`，其内部别名是 `cm-{随机id}`，**没在 ORM 的连接注册表里登记**。
- 当 LangChain 后续用 ORM 的 `Collection(..., using=...)` 操作时，ORM 找不到该别名，抛 `ConnectionNotExistException: should create connection first`。

补丁的做法：劫持 `MilvusClient.__init__`，原始构造完成后把 `self._using` 强制改成 `"default"`——所有 MilvusClient 都挂在 ORM 已注册的 `default` 连接上，两套 API 共用一条连接。用 `_done` 属性保证只补丁一次。

这类补丁是"生态磨合成本"的体现：修不了上游，只能在己方边界上做兼容。注释写得很完整。

### 2.4 Collection Schema（milvus_client.py:158-199）

`biz` 集合四个字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | VARCHAR(100) | 主键 |
| `vector` | FLOAT_VECTOR(1024) | 向量，L2 距离 |
| `content` | VARCHAR(8000) | 文档块原文（注意 8000 上限，超长会截断/报错） |
| `metadata` | JSON | 来源文件、章节等元信息 |

索引策略分模式（milvus_client.py:206-218）：Lite 只支持 **FLAT**（暴力全扫，数据少时没问题）；服务端用 **IVF_FLAT**（倒排聚类，`nlist=128`，把向量分 128 个簇，检索时只扫最近几个簇，大数据量下快很多）。

### 2.5 其余细节

- `_load_collection()`（milvus_client.py:227-262）：兼容多版本 pymilvus 的防御性编程——先试新版 `utility.load_state`，`AttributeError` 就退化为"直接 load 并捕获 already loaded 异常"。
- `health_check()`（milvus_client.py:278-298）：注意实现是 `connections.list_connections()`——**查的是本地连接注册表，并没有真正 ping 服务器**。Milvus 若在连接之后挂掉，健康检查仍可能报 healthy，这是 `/health` 接口的隐藏弱点。
- `close()`（milvus_client.py:300-323）：release + disconnect 各自独立 try，错误收集后统一报——一个失败不挡住另一个。
- 实现了 `__enter__`/`__exit__` 支持 `with` 语法，但项目里没用到（都走单例）。

## 3. 已删除的 `llm_factory.py`（历史记录）

该文件曾是一个 LLM 工厂：`create_chat_model()` 静态方法返回 LangChain 的 `ChatOpenAI`，`base_url` 指向阿里云 **OpenAI 兼容端点**（`dashscope.aliyuncs.com/compatible-mode/v1`），意图是"换模型提供商时只改 `base_url` 和 `api_key`"。

但全项目没有任何地方 import 它——真实的 LLM 创建全部走 `langchain_qwq` 的 `ChatQwen`（`rag_agent_service.py:95`、`agent/aiops/planner.py:122`、`executor.py:50`、`replanner.py:138,165`），因为 `ChatQwen` 对通义千问的流式、思考模式（QwQ）支持更好。这是典型的架构演化痕迹：**先设计了"可切换 provider"的抽象，后来为了用好某个具体 provider 的特性，全员绕过了抽象**。

已连同 README 中的结构说明一并删除。如果未来真要支持多 provider，更合理的方向是反过来收敛：让各服务统一从一个工厂取 `ChatQwen` 实例，而不是回到 OpenAI 兼容层。

## 4. 本层小结

- core 层现在只做一件事：Milvus 连接的封装与生命周期管理（全局单例 `milvus_manager`）。
- LLM 创建没有统一封装，各服务直接用 `ChatQwen`——了解这点后读服务层代码不会困惑"工厂在哪"。
- 两个要记住的运行时行为：**维度不匹配会删库重建**、**health_check 查的是本地注册表而非真实连通性**。
- 猴子补丁是理解后续 RAG 服务链的前置知识——它解释了为什么 LangChain 的 Milvus 集成能和项目自管的 ORM 连接共存。

---

# 模块五：RAG 服务链（`app/services/` 向量五件套）

涉及文件：`vector_embedding_service.py`、`document_splitter_service.py`、`vector_index_service.py`、`vector_store_manager.py`。这一层是**知识库能力的核心**——从"上传一个 md 文件"到"对话能引用它"的完整链路都在这里。

> 注：本层曾存在 `vector_search_service.py`（原生 pymilvus 检索路径），因从未被使用已删除，详见 2.5 节。

## 1. 链路全景：写入与读取两条路

RAG 本质是两条流水线，这五个文件分别站在不同环节上：

```
【写入路径：文档 → 向量库】（/api/upload 触发）

 uploads/xx.md
     │
     ▼
 vector_index_service.index_single_file()        ← 编排者
     │  ① 读文件文本
     │  ② delete_by_source() 先删该文件旧数据      ← "覆盖更新"的关键
     │  ③ document_splitter_service.split_document()
     │        │  .md → Markdown标题切分 → 二次切分 → 合并小碎片
     │        │  .txt → 递归字符切分
     │        ▼
     │     List[Document]（每块带 metadata: _source/_file_name/...）
     │  ④ vector_store_manager.add_documents()
     │        │  生成 uuid 作 id
     │        ▼
     │     LangChain Milvus ──内部调用──→ vector_embedding_service
     │                                     .embed_documents()
     │                                     （调 DashScope 拿 1024 维向量）
     ▼
 Milvus collection "biz"（id/vector/content/metadata）

【读取路径：提问 → 相关文档】（对话时由 Agent 触发）

 用户问题
     │
     ▼
 knowledge_tool（app/tools/，见模块九）
     │  vector_store_manager.get_vector_store()
     ▼
 LangChain Milvus.similarity_search()
     │  内部调 embed_query() 把问题变成向量
     ▼
 Milvus 返回 top_k=3 个最相似文档块
```

## 2. 逐文件解析

### 2.1 `vector_embedding_service.py`：向量化

`DashScopeEmbeddings`（vector_embedding_service.py:12）实现了 LangChain 的标准 `Embeddings` 接口（`embed_documents` 批量 / `embed_query` 单条），内部用 **OpenAI SDK 走 DashScope 兼容端点**调 `text-embedding-v4`，输出 1024 维。

- 实现标准接口的价值：可直接作为 `embedding_function` 参数塞给 LangChain Milvus（vector_store_manager.py:46），嵌入动作由 LangChain 在 `add_documents`/`similarity_search` 内部自动触发，业务代码无感知。
- 细节：`_mask_api_key()`（vector_embedding_service.py:51-56）把密钥打码后才进日志（形如 `sk-***...***`）。
- ⚠️ **导入期炸弹**：文件末尾的全局单例在 import 时就构造，API Key 为空会直接 `raise ValueError`（vector_embedding_service.py:34-35）——**没配 `DASHSCOPE_API_KEY`，整个应用 import 阶段就崩**，不是等到第一次调用。

### 2.2 `document_splitter_service.py`：切分策略

三阶段流水线（document_splitter_service.py:45-77），知识库质量的隐形决定者：

- **第一阶段 `MarkdownHeaderTextSplitter`**：只按 `#` 和 `##` 标题切（特意不按三级标题，"避免过度碎片化"），`strip_headers=False` 保留标题原文——让分片**按语义章节对齐**，而不是按字数硬切。
- **第二阶段 `RecursiveCharacterTextSplitter`**：超长章节按字符递归再切。注意 `chunk_size` 用的是 `config.chunk_max_size * 2 = 1600`（document_splitter_service.py:33）——配置里写的 800 实际没直接用，真实块上限是 1600 字符。
- **第三阶段 `_merge_small_chunks`**：把小于 300 字符的碎片合并到前一块（上限 1600），避免"一句话章节"产生无意义的小向量。

切分产出是 **LangChain 的 `Document`**（`page_content` + `metadata`）——这回答了模块三留下的问题：项目自定义的 `DocumentChunk` 确实没人用，LangChain 的 `Document` 已是事实标准。

### 2.3 `vector_store_manager.py`：LangChain ↔ Milvus 的桥

- `__init__` 里就调用 `_initialize_vector_store()`（vector_store_manager.py:25），即 **import 时就连接 Milvus 并创建 LangChain `Milvus` VectorStore**，早于 FastAPI lifespan——这解释了 `milvus_client.connect()` 为什么强调幂等（milvus_client.py:69 的注释），也解释了启动日志里 Milvus 连接先于 uvicorn 就绪。
- VectorStore 字段映射（vector_store_manager.py:51-54）对应模块四的 schema：`text_field→content`、`vector_field→vector`、`primary_field→id`、`metadata_field→metadata`。
- `add_documents()` 用 `uuid4` 给每个分片生成 id（`auto_id=False`），并打耗时日志。
- `delete_by_source()`（vector_store_manager.py:99-125）用 ORM 的 `collection.delete` + JSON 路径表达式 `metadata["_source"] == "<文件路径>"` 删除某文件全部旧分片——**"重新上传同名文件 = 先删旧再插新"** 的覆盖语义就靠它。
- 降级设计：Milvus 不可用时 `vector_store = None` 只记日志不抛异常（vector_store_manager.py:62-65），应用照常启动，知识库返回空。

### 2.4 `vector_index_service.py`：写入编排者

- `index_single_file()`（vector_index_service.py:131-171）就是写入路径的四步编排：读文件 → 删旧 → 切分 → 入库。
- `index_directory()`（vector_index_service.py:67-129）批量版，配了手写的 **`IndexingResult`** 结果类（vector_index_service.py:13-56）：成功/失败计数、失败文件清单、耗时统计——`/api/index_directory` 返回的 `result.to_dict()` 就是它。这个类和模块三的死代码模型是**两种命运**：它是真被用的手写数据结构（普通 class，非 Pydantic）。
- 失败隔离：单文件失败不影响其他文件（try/except 在循环内）。

### 2.5 已删除的 `vector_search_service.py`（历史记录）

该文件曾实现一条"原生 pymilvus 检索路径"（手动 `embed_query` → `collection.search`，L2 距离 + `nprobe=10`），但**没有任何文件 import 它**——项目真实检索走 LangChain 路径（`knowledge_tool` → `get_vector_store()`）。同样，`VectorStoreManager.similarity_search()` 方法也没人调用（`knowledge_tool` 直接拿 vector_store 对象自己检索）。

产生原因和 `llm_factory` 一样：早期手写的原生实现，后来被 LangChain 集成取代，旧的没删。两者均已删除（含 README 结构树对应行），删除后 `import app.main` 正常、全部测试通过。

services 层当前状态：

| 文件 | 状态 |
|------|------|
| `vector_embedding_service.py` | ✅ 在用（被 LangChain Milvus 内部调用） |
| `document_splitter_service.py` | ✅ 在用 |
| `vector_index_service.py` | ✅ 在用（`/api/upload` 链路） |
| `vector_store_manager.py` | ✅ 在用 |
| ~~`vector_search_service.py`~~ | 🗑 已删除（原为死代码） |

## 3. 本层小结

- 写入链路完整且设计合理：标题感知切分、小碎片合并、先删后插的覆盖语义、批量容错。
- **导入期副作用是这层最大的设计味道**：embedding 单例没 key 会崩、store 单例 import 时就连库——`main.py` lifespan 里连接 Milvus 那段其实是"第二次连接"（幂等返回）。
- 经过本轮清理，本层五个文件剩四个，全部真实在用，无死代码。

---

# 模块六：RAG Agent 服务（`rag_agent_service.py`）

这个文件是 `/api/chat` 和 `/api/chat_stream` 背后的"大脑"——用户问的每个问题都在这里经过 LLM + 工具的多轮协作后生成答案。

## 1. 它由什么组成

```
RagAgentService（全局单例 rag_agent_service，rag_agent_service.py:418）
 │
 ├─ ChatQwen 模型           ← qwen-max，temperature=0.7，流式开启
 │    （rag_agent_service.py:95，注意：不走已删除的 llm_factory）
 │
 ├─ 工具集 = 本地工具 + MCP 工具
 │    ├─ 本地 3 件（DEFAULT_LOCAL_AGENT_TOOLS）：
 │    │    retrieve_knowledge      查知识库（RAG 检索入口）
 │    │    get_current_time        当前时间
 │    │    query_prometheus_alerts 查 Prometheus 告警
 │    └─ MCP 工具（首次查询时异步加载，失败降级为仅用本地工具）
 │
 ├─ MemorySaver checkpointer  ← 会话记忆（内存版，重启丢失）
 │
 └─ create_agent(model, tools, checkpointer)
      ← LangChain v1 预制的 ReAct Agent，内部是一张状态图
```

`create_agent` 创建的是一个 **ReAct（Reasoning + Acting）循环图**：LLM 思考 → 决定调工具 → 执行工具 → 结果喂回 LLM → 再思考……直到 LLM 给出最终回答：

```
        ┌──────────────────────────────────────┐
        ▼                                      │
 用户问题 → LLM 节点 → 要调工具？ ──是──→ 工具节点 ─┘
                        │
                        否（得到答案）
                        ▼
                     输出回答
```

知识库的 RAG 检索**不是一个独立步骤，而是被包装成 `retrieve_knowledge` 工具**交给 LLM 自主决定何时调用——LLM 判断"这个问题需要查文档"才会触发模块五的检索链路。这是"Agent 化 RAG"和"固定流水线 RAG"的本质区别。

## 2. 延迟初始化：`__init__` 和 `_initialize_agent` 分离（rag_agent_service.py:117-154）

一个值得理解的模式：`__init__` 是同步的，但 MCP 工具加载是**异步**的（要连 MCP 服务器），所以构造函数里只备好模型和本地工具，真正的 `create_agent` 推迟到**第一次查询时**（`_initialize_agent()`，用 `_agent_initialized` 标志保证只跑一次）。

MCP 工具加载失败只降级不崩溃（rag_agent_service.py:132-136）：日志警告后仅用本地工具继续——和 Milvus 的降级哲学一致。这也解释了为什么 MCP 服务没启动时对话功能依然可用。

## 3. 两种查询方式

### `query()`：非流式（rag_agent_service.py:186-249）

```
[SystemMessage(系统提示), HumanMessage(问题)]
        │
        ▼
agent.ainvoke(config={"configurable": {"thread_id": session_id}})
        │   ← thread_id 是关键：同一 session_id 的多轮对话
        │     会被 checkpointer 自动串起来
        ▼
取 result["messages"][-1].content 作为答案
```

### `query_stream()`：流式（rag_agent_service.py:251-319）

核心是 `agent.astream(stream_mode="messages")`——LangGraph 按 **token 级**吐出 `(消息块, 元数据)` 对。服务只挑 `AIMessageChunk` 里的文本块往外发：

```python
async for token, metadata in self.agent.astream(..., stream_mode="messages"):
    if type(token).__name__ in ("AIMessage", "AIMessageChunk"):
        for block in token.content_blocks:
            if block.get('type') == 'text':        # 只发文本块
                yield {"type": "content", "data": text, "node": 节点名}
yield {"type": "complete"}
```

`node` 字段带上了 LangGraph 节点名（`model`/`tools`），这就是 API 层 `debug` 事件的来源。

## 4. 会话持久化：MemorySaver 三操作

- **写入**：`ainvoke`/`astream` 传入 `thread_id` 后由 checkpointer 自动完成，业务代码无感知。
- **读取** `get_session_history()`（rag_agent_service.py:321-384）：直接翻 checkpointer 内部结构——`checkpoint["channel_values"]["messages"]`，跳过 SystemMessage，转成 `[{role, content, timestamp}]`。**一个小毛病**：LangChain 消息对象没有 `timestamp` 属性，`getattr(msg, 'timestamp', None)` 永远是 None，所以所有时间戳都是**查询那一刻的 `datetime.now()`**（rag_agent_service.py:376）——历史消息的时间全是假的。
- **清空** `clear_session()`（rag_agent_service.py:386-405）：`checkpointer.delete_thread(session_id)`。

"会话存内存、重启即丢"就是 `MemorySaver` 的性质决定的——要持久化可换成 `SqliteSaver` 等实现，接口不变。

## 5. 两个已发现的问题

**① 消息修剪中间件：已接线 ✅（修复记录）**

文件开头的 `trim_messages_middleware`（rag_agent_service.py:42-83）意图是只保留系统消息 + 最近 3 轮对话，防止长对话撑爆上下文窗口。但最初 `create_agent` 调用时没有传 `middleware=` 参数，该函数从未执行——长对话会把全部历史每次原样发给 LLM，token 消耗随轮次线性增长。

**修复**：该函数原本是给旧版 langgraph `pre_model_hook` 写的（单参数签名），LangChain 1.3 的 `create_agent` 走 `@before_model` 中间件协议（`(state, runtime)` 双参数）。已加 `@before_model` 装饰器、补 `runtime` 参数，并在 `create_agent` 中传 `middleware=[trim_messages_middleware]` 完成接线。

类型注解上还有一个坑：`@before_model` 是泛型装饰器，`StateT` **bound 到 LangChain 自己的 `AgentState`**（`messages: list[AnyMessage]` + `jump_to` 等字段），项目原先自定义的同名 `AgentState` TypedDict 不满足约束，pyright 会报"重载与提供的参数不匹配"。修复时删掉了自定义 TypedDict，改用 `from langchain.agents.middleware import AgentState, Runtime`——与装饰器文档示例一致，pyright 检查 0 错误。

已验证：三种消息数场景（5/9/10 条）的修剪逻辑正确、`create_agent` 接受该中间件、`import app.main` 正常、pyright 0 错误、全部测试通过。

⚠️ 遗留边界情况：修剪按"奇偶条数"估算轮次，若切口恰好落在"AIMessage(带 tool_calls) 与它的 ToolMessage 结果"之间，会把不完整的消息对发给模型，DashScope 可能报 400。工具调用密集的超长对话才容易碰到，如需根治要把修剪逻辑升级为"工具调用对感知"。

**② 流式事件契约前后端脱节（待处理）**

服务层实际只产出三种事件：`content`、`complete`、`error`。但 API 层 `chat.py` 里为 `debug`/`tool_call`/`search_results` 写的分支**永远不会被触发**（当前版本服务不产出这些类型）。而且 `complete` 事件的 data 是空的（`yield {"type": "complete"}`，rag_agent_service.py:316），API 层 docstring 里承诺的 `done` 载荷 `{"answer":..., "tool_calls":[...]}` 实际是 `null`。处理方式待看前端 `static/app.js` 实际消费什么后定（见模块九）。

## 6. 本模块小结

- 架构本质：**LangChain v1 `create_agent` 预制 ReAct 图 + ChatQwen + MemorySaver**，RAG 检索被工具化，由 LLM 自主决策调用。
- 会话管理完全委托给 checkpointer，`thread_id = session_id` 是贯穿 API 层和服务层的线索。
- 延迟初始化解决了"同步构造函数需要异步资源（MCP 工具）"的矛盾。
- 修剪中间件已接线（`@before_model`），长对话上下文有界；遗留"工具调用对可能被切断"的边界情况。
- 流式事件契约前后不一致（待模块九看过前端后统一处理）。

---

# 模块七：AIOps 诊断引擎（`app/agent/aiops/` + `aiops_service.py`）

涉及文件：`state.py`、`planner.py`、`executor.py`、`replanner.py`、`utils.py`，外加组装者 `services/aiops_service.py`。这是 `/api/aiops` 接口背后的诊断大脑，实现了经典的 **Plan-Execute-Replan** 模式（LangGraph 官方教程的改良版）。

## 1. 全景：一张状态图跑诊断

和模块六的 ReAct"自由循环"不同，这里是**手工搭建的固定拓扑状态图**（aiops_service.py:29-79）：

```
                    ┌─────────────────────────────────────┐
                    │                                     │
                    ▼                                     │
 ┌─────────┐   ┌──────────┐   ┌───────────┐              │
 │ Planner │──→│ Executor │──→│ Replanner │              │
 │ 定计划   │   │ 执行一步  │   │ 评估决策   │              │
 └─────────┘   └──────────┘   └─────┬─────┘              │
                                    │                     │
                     ┌──────────────┼──────────────┐      │
                     ▼              ▼              ▼      │
                 continue ──→ 回 Executor  ────────┘      │
                 replan   ──→ 替换剩余计划后回 Executor     │
                 respond  ──→ 写 response → END（出报告）  │
```

三个节点共享一份状态 `PlanExecuteState`（state.py:10-23），四个字段：

| 字段 | 类型 | 作用 |
|------|------|------|
| `input` | str | 任务描述（诊断 prompt） |
| `plan` | List[str] | 剩余步骤列表（每执行一步就少一个） |
| `past_steps` | `Annotated[List[tuple], operator.add]` | 已执行的 (步骤, 结果)——`operator.add` 是让 LangGraph **追加而非覆盖**的 reducer 写法 |
| `response` | str | 最终报告；**非空即流程结束**的判断依据 |

## 2. Planner：带着"经验"定计划（planner.py）

`planner()` 做四件事（planner.py:63-158）：

1. **先查知识库**：拿任务描述去 `retrieve_knowledge` 检索相关运维经验文档，塞进 prompt 的 `{experience_context}`——这是 RAG 在诊断链路的第二次出现（第一次在对话链路）。
2. 汇总工具清单（本地 3 件 + MCP 工具），格式化成文本塞进 `{tools_description}`，让 LLM 知道"有哪些工具可用"来定计划。
3. `ChatQwen(temperature=0)` + `with_structured_output(Plan)`——**结构化输出**，强制 LLM 返回 `{"steps": [...]}` 而不是自由文本。`temperature=0` 求稳定。
4. 两道容错：structured output 偶发返回 None（注释直言"LLM 抖动"），**最多重试一次**；彻底失败则返回兜底计划 `["收集相关信息", "分析数据", "生成报告"]`——计划再烂也不让流程崩。

## 3. Executor：一次只执行一步（executor.py）

每次进入取出 `plan[0]` 执行，然后 `plan` 变 `plan[1:]`、`past_steps` 追加 `(步骤, 结果)`（executor.py:101-105）。执行方式是一个**手写的两轮工具循环**：

```
当前步骤 → LLM(绑定全部工具) → 决定调工具？
              │
              ├─ 不调 → 直接把 LLM 回答当步骤结果
              ▼
        ToolNode 自动执行工具调用
              ▼
        工具结果喂回 LLM → 生成步骤结果
```

两个细节：

- **只跑一轮工具调用**（executor.py:79-97）：第二轮 LLM 如果又请求调工具，代码不会再执行，直接取 `content`——复杂步骤可能拿到空内容。这是和模块六 ReAct 循环（不限轮数）的能力差距。
- 步骤失败不中断流程：异常被捕获后记为 `(task, "执行失败: ...")` 写进 `past_steps`（executor.py:107-111），让 replanner 看到失败事实再决策。

## 4. Replanner：全模块设计最用心的部分（replanner.py）

每执行完一步，replanner 用结构化输出 `Act`（continue/replan/respond 三选一 + 可选 `new_steps`）做决策。亮点在**防失控的护栏设计**（replanner.py:134-238），明显是和 LLM 斗智斗勇后打上的补丁：

```
硬规则（代码强制，不靠 LLM 自觉）：
  ① past_steps >= 8  → 无条件强制生成报告（MAX_STEPS 硬顶）
  ② past_steps >= 5  → 禁止 replan，只能 respond
  ③ replan 新步骤数 > 预算(8 - 已执行) → 强制截断

软引导（prompt 提示词）：
  "任务完成守卫"——变更类任务"计划里写过 ≠ 已完成"，
  未 commit、未验证就不许 respond（replanner.py:56-60）
```

③ 的注释（replanner.py:222-224）记录了一个真实踩过的坑：预算必须是 `8 - 已执行` 而不是"当前剩余步骤数"，否则**每次 replan 步骤都被截得越来越少，直至计划丢失**。

决策后：respond → `_generate_response()` 生成最终报告（同样结构化输出 `Response`，失败还有一份 Markdown 兜底模板 replanner.py:296-307）；replan → 替换剩余 `plan`；continue → 返回 `{}` 不动状态。然后图的条件边 `should_continue`（aiops_service.py:49-64）看 `response` 非空就 END，否则回 executor。

## 5. `aiops_service.py`：组装 + 事件翻译

服务做三件事：

1. **组构图**（第 1 节的拓扑），编译时挂 `MemorySaver`。
2. **`execute()` 流式执行**（aiops_service.py:81-157）：`stream_mode="updates"` 按节点吐状态增量，三个 `_format_*_event` 方法把节点输出翻译成 API 层的事件（`plan` / `step_complete` / `report` / `status`）——模块二讲的 SSE 事件就是在这里成型的。
3. **`diagnose()` 兼容包装**（aiops_service.py:159-262）：塞给 `execute()` 一段**固定的诊断任务 prompt**——内嵌一整份告警分析报告 Markdown 模板（活跃告警清单表、根因分析、处理方案、结论四段式），还写了"严禁编造、失败要如实说明"。`complete` 事件在这里被改写成 `diagnosis` 字段格式。

注意：**并没有独立的"拉取告警"阶段**——任务 prompt 第一句"诊断当前系统是否存在告警"，由 agent 自己调 `query_prometheus_alerts` 工具完成。API 层 docstring 里的 `fetching_alerts` 阶段在代码中不存在，是文档想象出来的。

## 6. 发现的问题（一已修复 + 两个瑕疵）

- **~~⚠️ 真实 bug：同 session_id 重复诊断会状态污染~~（已修复 ✅）**。`execute()` 的初始状态里 `past_steps: []` 走的是 `operator.add` reducer——**传入空列表 = 追加零条 = 旧历史原样保留**。即用同一个 session_id（API 层默认 `"default"`！）调第二次 `/api/aiops`，`past_steps` 会带上一次的全部历史，replanner 的步数统计（≥5 禁 replan、≥8 强出报告）直接被旧数据抬高，第二次诊断可能跑一两步就被强制收尾。**修复**：`execute()` 开头加 `await self.checkpointer.adelete_thread(session_id)`（aiops_service.py:100-104），每次诊断从干净状态开始。已用真实编译图验证：修复前 `update_state` 写入的旧 `past_steps` 跨执行残留（复现成功），执行删除后 `get_state` 返回空。
- **效率瑕疵**：planner/executor/replanner **每次节点调用都重新 `new ChatQwen` + 重新拉一遍 MCP 工具列表**（executor 每步一次），没有复用。
- **文档失真**：API docstring 的 `fetching_alerts` 阶段不存在；`evidence` 字段也没人发。

## 7. 本模块小结

- Plan-Execute-Replan 三节点固定拓扑，状态四字段，`response` 非空即停。
- 工程质量集中在 replanner 的**防失控护栏**（8 步硬顶、5 步禁 replan、预算截断）和 planner/response 的**双重重试 + 兜底**。
- RAG 知识库以"经验文档"形式参与计划制定——知识库同时服务对话和诊断两条链路。
- 待办：~~session 状态污染 bug~~（已修复，execute 开头删除旧检查点）、节点级 LLM/MCP 重复初始化（可优化）、API 文档与实际事件对齐。

## 8. 深入 `aiops_service.py`（逐段精读）

该文件（347 行）身兼三职：**图的组装者**（`_build_graph`）、**流程的执行者**（`execute`）、**事件的翻译者**（`diagnose` + 三个 `_format_*_event`）。

结构地图：

| 行 | 内容 | 职责 |
|---|---|---|
| 14-17 | 节点名常量（`NODE_PLANNER` 等） | 避免魔法字符串 |
| 23-27 | `__init__`：MemorySaver + 构图 | 组装（import 即建图） |
| 29-79 | `_build_graph()` + `should_continue()` | 组装 |
| 81-163 | `execute()` 通用任务流 | 执行 |
| 165-268 | `diagnose()` AIOps 入口 | 业务包装 |
| 270-343 | 三个 `_format_*_event()` | 事件翻译 |
| 347 | 全局单例 `aiops_service` | — |

### 8.1 条件边 `should_continue`（49-64）逐分支

```python
if state.get("response"):   return END            # ① 有报告 → 结束
if state.get("plan", []):   return NODE_EXECUTOR  # ② 还有步骤 → 继续执行
                            return END            # ③ 兜底 → 结束
```

③ 的注释写着"返回 replanner 生成响应"，代码却是 `return END`——**注释和代码不一致**。但该分支实际不可达：replanner 在 `plan` 为空时一定会写 `response`（replanner.py:251）。死分支 + 错注释，无害但误导读者。

`add_conditional_edges` 的第三个参数（66-73）是**路由映射表**：返回值先查表再跳转，允许"返回值"和"节点名"解耦。这里键值相同，属于直白用法。

### 8.2 `execute()` 与 `stream_mode="updates"` 语义

核心循环（120-137）：`astream(stream_mode="updates")` 每跑完一个节点吐一个 `{节点名: 状态增量}`，交给对应 formatter 翻译成 SSE 事件后 `yield`。与模块六对比：

| | `stream_mode="messages"`（rag_agent） | `stream_mode="updates"`（本文件） |
|---|---|---|
| 粒度 | LLM 每吐一个 token 发一次 | 每跑完一个**节点**发一次 |
| 内容 | (消息块, 元数据) | `{节点名: 该节点返回的状态增量}` |
| 前端效果 | 打字机 | 阶段进度条 |

图跑完后 `get_state()` 取最终 `response` 包成 `complete` 事件（139-153）；异常转成 `error` 事件而不是抛出（157-163），保证 SSE 连接优雅收尾。

### 8.3 进度计数 bug：已修复 ✅（修复记录）

用小图实验实锤：`updates` 模式吐的是**节点返回的增量**，`past_steps` 里永远只有刚追加的 1 条（不是累计全量）。修复前 `_format_executor_event` 的进度计算（aiops_service.py:299）：

```python
f"步骤执行完成 ({len(past_steps)}/{len(past_steps) + len(plan)})"
#              ↑ 增量恒为 1        ↑ 还随 plan 缩短而缩小
```

6 步计划实际显示 `(1/6)、(1/5)、(1/4)…`——分子永远为 1，分母越缩越小；API docstring 示例 `(2/6)` 永远不会出现。

**修复**：事件循环仍用 `updates` 拿节点名，但每个节点事件到来后用 `await self.graph.aget_state(config_dict)` 取**合并后的全量状态**传给 formatter（execute 事件循环内）。选择此方案而非换成 `stream_mode="values"`，因为 `values` 模式的事件不带节点名，整个事件分发结构都得重构。

**附带收益**：replanner 做出 continue 决策返回 `{}` 时，原先因空增量 falsy 而误报"评估节点运行中"（见 8.5）；现在 formatter 拿到的是全量状态（`plan` 非空），正确显示"评估完成，继续执行剩余步骤"。该文案问题一并消除。

**验证**：无 LLM 的小循环图实验显示全量进度 `(1/3)→(2/3)→(3/3)` 正确累计；`_format_executor_event` 传入全量状态输出"步骤执行完成 (2/4)"；27 个测试通过，pyright 0 错误。formatter 中 `if not state:` 的防御分支因不再收到空增量而成为死分支，无害保留。

### 8.4 `diagnose()`（165-268）：业务入口包装

注释自称"兼容旧接口"——揭示真实设计：**`execute()` 是通用 Plan-Execute-Replan 执行器，`diagnose()` 只是它的预设**。做两件事：

1. **塞固定任务 prompt**（174-246）：内嵌告警分析报告 Markdown 模板（活跃告警清单 → 根因分析 → 处理方案 → 结论），外加"纯 Markdown、严禁编造、失败如实说明"三条硬约束。诊断质量很大程度由这段 prompt 决定。
2. **转换完成事件**：把 `execute()` 的 `{"type":"complete","response":...}` 改写为前端契约 `{"type":"complete","stage":"diagnosis_complete","diagnosis":{"status":"completed","report":...}}`，其余事件透传。

### 8.5 三个 formatter（270-343）

| 方法 | 输入 | 产出事件 |
|---|---|---|
| `_format_planner_event` | planner 的 `{"plan": [...]}` | `plan`（带步骤清单）；空则 `status` |
| `_format_executor_event` | executor 增量 `{plan, past_steps}` | `step_complete`（含坏进度）；空则 `status` |
| `_format_replanner_event` | replanner 增量 | 有 response → `report`；有 plan → `status`；返回 `{}`（continue 决策）时因 falsy 报"评估节点**运行中**"（文案瑕疵，评估其实已完成） |

### 8.6 小结

理解这个文件的钥匙是两个 LangGraph 语义：**`updates` 吐增量**（进度 bug 的根源）和**条件边路由表**。设计亮点是 `execute()`/`diagnose()` 的通用/特化分离；待修：~~进度计数~~（已修复，事件循环内取全量状态）、~~continue 事件文案~~（随修复一并消除）、`should_continue` 错注释。

---

# 模块八：MCP 集成（`app/agent/mcp_client.py` + `mcp_servers/`）

## 1. MCP 在这套系统里的角色

MCP（Model Context Protocol）是"**工具提供方**"和"**工具消费方（Agent）**"之间的标准协议。在本项目中：

```
┌─────────────────────────── 主进程（FastAPI，9900）──────────────────────────┐
│                                                                            │
│  RAG Agent / AIOps 引擎                                                    │
│       │ 调用工具                                                            │
│       ▼                                                                    │
│  mcp_client.py（MultiServerMCPClient 全局单例）                              │
│       │  HTTP streamable-http 协议（langchain-mcp-adapters 把 MCP 工具       │
│       │  包装成 LangChain BaseTool，Agent 无感知）                            │
└───────┼────────────────┬──────────────────┬────────────────────────────────┘
        ▼                ▼                  ▼
  cls_server.py    monitor_server.py   firewall_server.py
  (FastMCP:8003)   (FastMCP:8004)      (FastMCP:8005)
  日志查询          监控指标            有状态防火墙演练
```

价值：**工具跑在独立进程里**，挂了不拖垮主服务（配合客户端降级），增加工具不用改主应用——主应用只认 `config.mcp_servers` 里的三个 URL。

## 2. 客户端：`mcp_client.py`（230 行，全是工程细节）

这个文件没有业务逻辑，全是"让工具调用变可靠"的基础设施：

- **全局单例 + 延迟初始化**（mcp_client.py:17, 112-155）：`_mcp_client` 模块级变量，第一次使用时才创建。注释记录了一个版本事实：`langchain-mcp-adapters` 0.1.0 起 `MultiServerMCPClient` **不再能当上下文管理器用**，直接创建即可。
- **重试拦截器** `retry_interceptor`（mcp_client.py:46-102）：客户端最值钱的设计——以**拦截器**形式包在每次工具调用外面，指数退避重试 3 次（1s→2s→4s）；全失败后**不抛异常**，而是返回 `isError=True` 的 `CallToolResult`——LLM 会把失败当成普通工具结果读进去，自行决定换工具或在报告里说明，**Agent 循环不会因工具失败而崩**。
- **异常链展开** `format_exception_chain`（mcp_client.py:20-32）：MCP 的异步错误常被打包成 `ExceptionGroup`（Python 3.11 TaskGroup 风格），日志里直接打 `str(e)` 看不到真因，这个函数递归展开成可读的树。API 层 chat.py 也 import 了它。
- **配置体检** `suggest_mcp_transport`（mcp_client.py:214-230）：URL 含 `/sse/` 但配了 `streamable-http`（或反过来）时给警告但不自动改——腾讯云托管 MCP 用 sse、本地 FastMCP 用 streamable-http，这个函数防止配错。
- **`get_mcp_client_with_retry`**（mcp_client.py:158-186）：对外主入口，把重试拦截器放在拦截器链最前面。RAG Agent 和 AIOps 三节点用的都是它。

## 3. 服务器侧：三个 FastMCP 服务

三者共享同一套模式：`FastMCP("名字")` + `streamable-http` + `/mcp` 路径 + 各自复制了一份 `log_tool_call` 装饰器（把每次工具调用的参数和结果打进 `mcp_*.log`——项目根目录的三个 log/pid 文件就是它们的）。

### CLS 日志服务（8003，5 个工具）

`get_current_timestamp`、`get_region_code_by_name`、`get_topic_info_by_name`、`search_topic_by_service_name`、`search_log`。mock 数据是**写死的主题表**（data-sync-service 对应 topic-001/002、api-gateway-service 对应 topic-003）。注意一个**坑**：topic-002 名叫"数据同步服务**错误日志**"，但 `search_log` 只给 topic-001 生成数据，查 topic-002 返回"主题不存在"——Agent 若循着"查错误日志"的直觉找到 topic-002 再 search，会得到报错。

### Monitor 监控服务（8004，2 个工具）

`query_cpu_metrics`、`query_memory_metrics`。mock 不写死样本，而是**算法生成**：不管查哪个服务，CPU 都返回一条"从 ~10% 线性爬到 ~96% 加随机抖动"的曲线（超 80% 触发告警标记），内存爬到 85%（超 70% 告警）。**这就是为什么 AIOps 诊断永远能诊断出"CPU 高"故障**——演示场景内置在 mock 算法里。

### Firewall 防火墙（8005，11 个工具 + 4 个管理路由）——三者里最用心的

为"配置变更全链路演练 + 自动化评测"设计的**有状态假设备**，核心是 `FirewallState` 类（firewall_server.py:91-472），MCP 工具只是薄封装：

```
读配置                     写操作（全落在 candidate）        验证
get_firewall_overview      add/update/delete/move_rule      get_config_diff
list_security_zones        ─────────────────────────        test_traffic（首条命中）
list_firewall_rules        commit_config ←── 两阶段提交      get_rule_hit_count
get_firewall_rule          discard_candidate（回滚）
        │                           │
        └────── running_rules ←─────┘ commit 时才生效
               （test_traffic 以 running 为准）
```

- **两阶段提交**：写操作只改 candidate；`commit_config` 才把 candidate 深拷贝到 running、revision+1。commit 前 `test_traffic` 看不到改动——和真实防火墙（华为/思科 candidate-vs-running）语义一致。
- **故障注入**（firewall_server.py:334-354）：`commit_reject`（永远被拒）、`commit_flaky`（前 N 次"设备繁忙"后恢复，**测 Agent 的重试**）、`commit_lose`（**实际生效但返回超时失败**，带 hint 提示用 overview/diff 核实——**测 Agent 面对"结果歧义"会不会核实真实状态**）。这是为 Agent 能力评测设计的精妙陷阱。
- **管理通道**：用 FastMCP 的 `@mcp.custom_route` 挂了 `/admin/reset|scenario|snapshot|health` 四个 HTTP 路由——与 MCP 工具同端口但**不暴露给 Agent**，专供评测脚本注入故障、导出快照打分。
- **审计日志**：每次操作（成败都记）带 timestamp/operation/params/result/detail + 当时的 revision，`commit_lose` 额外写明"实际已生效"——评测方能区分"设备真实状态"和"Agent 看到的表象"。
- 它是**全项目唯一有测试覆盖的模块**：`tests/test_firewall_server.py` 27 个测试，直接实例化 `FirewallState` 测，覆盖出厂状态、增删改移、commit/discard、首条命中、三种故障注入、审计日志。

## 4. 本模块小结

- 客户端三件宝：**拦截器式重试（失败降级为工具结果）**、异常链展开、transport 配置体检。
- 服务器侧是演示/评测导向的 mock：monitor 的"永远攀升的 CPU 曲线"保证 AIOps 演示必有故障可查；firewall 的两阶段提交 + 故障注入是**评测 Agent 工程能力的沙盒**。
- CLS 的 topic-002"错误日志主题查不到日志"是个坑（有意或无意），Agent 碰上会在报告里说"查询失败"——护栏设计（失败不中断）让它不至于崩。
- 数据联动只是"约定同一批服务名"，三服务间无真实共享状态。

---

# 模块九：工具集与前端（`app/tools/` + `static/`）

## 1. 三个本地工具（`app/tools/`）

三个工具组成 `DEFAULT_LOCAL_AGENT_TOOLS`（`app/tools/__init__.py`），同时服务对话 Agent 和 AIOps 三节点。全部是 LangChain `@tool` 装饰器产物——**函数名即工具名，docstring 即工具说明书**（LLM 靠 docstring 决定何时调用，docstring 写得详细是有意的）。

### `retrieve_knowledge`（knowledge_tool.py）—— RAG 检索入口

- `@tool(response_format="content_and_artifact")`（knowledge_tool.py:13）：返回二元组——`content`（格式化文本）给 LLM 读，`artifact`（原始 `List[Document]`）给程序用。模块七 planner 注释"ainvoke 只返回 content"就是因为这个格式。
- 降级链完整：Milvus 没连上返回提示文字（knowledge_tool.py:30-32）、检索为空返回"没有找到相关信息"、异常**返回错误字符串而不是抛出**（knowledge_tool.py:50-52）——和 MCP 重试拦截器同一哲学：工具失败变成 LLM 能读懂的文字，Agent 循环永不因工具崩。
- `format_docs`（knowledge_tool.py:55-89）：格式化成"【参考资料 N】标题: h1 > h2 > h3 来源: 文件名 内容: ..."——模块五切分保留标题（`strip_headers=False`）的价值在这里兑现，LLM 能看到每段文字出自哪个章节。

### `get_current_time`（time_tool.py）

32 行小工具，`ZoneInfo` 做时区，默认 `Asia/Shanghai`。存在的意义：LLM 本身不知道"现在几点"，而 AIOps 诊断常需要"查最近 10 分钟日志"这类相对时间计算。

### `query_prometheus_alerts`（query_metrics_alerts.py）——写得最讲究的本地工具

拉 Prometheus `GET /api/v1/alerts`，亮点在细节：

- **去重键用完整 labels 的排序 JSON**（query_metrics_alerts.py:41-43），文件头 docstring 专门解释为什么不能按 `alertname` 去重——多实例同名告警会被错误合并。踩过坑才知道的写法。
- 输出做了**面向 LLM 的瘦身**：`common_labels` 只挑 severity/instance/namespace 等常见维度（减少 LLM 阅读成本），`duration` 把 RFC3339 时间换算成人话（`2h15m30s`），按激活时间从新到旧排序。
- 失败返回 `success=false` 的 JSON 字符串——同样是"错误即数据"的工具哲学。

## 2. 前端（`static/`）：单页两栏，无框架

`index.html`（132 行）+ `app.js`（1691 行）+ `styles.css`，第三方库只有 CDN 引入的 marked（Markdown 渲染）和 highlight.js（代码高亮）。单页两栏：左侧边栏（新建对话 + 历史列表），主区（消息流 + 输入区），右上角"AI Ops"按钮触发诊断。

关键机制：

- **SSE 用 `fetch` + `ReadableStream` 手动解帧，不是 EventSource**（app.js:760、1198）——证实模块二的判断：接口是 POST，原生 EventSource 用不了。解帧按 `\n` 切行、`data:` 行独立 `JSON.parse`、`event:` 字段读了但没用（后端统一发 `message`）。
- **双模式对话**：输入框可切"快速问答"（`/api/chat`）/"流式对话"（`/api/chat_stream`），默认快速。流式渲染是**每收到一片就对累积全文重新 `marked.parse`**——不是逐字打字机，是逐 chunk 全量重渲染。
- **会话管理是前后端混合**：session id 前端随机生成；历史列表存 localStorage（上限 50 条）；点开历史先调 `GET /api/chat/session/{id}` 拿后端 checkpointer 的，拿不到回退 localStorage。
- **上传**：~~前端限 50MB 校验，但后端只收 10MB~~（已对齐 ✅：前端校验改为与后端一致的 10MB，app.js:1118-1123）。

## 3. 流式事件契约的最终答案（模块六遗留问题）

前端消费方式完全核实后逐条对账：

| 事件 | 服务端实际发？ | 前端处理？ | 结论 |
|---|---|---|---|
| `content` | ✅ | ✅ 累积渲染 | 契约一致 |
| `done`（data） | ✅ 但 data=null | ❌ 从不读 data，用自己累积的全文 | 无影响，docstring 吹牛而已 |
| `tool_call`/`search_results`/`debug` | ❌ 不发 | ❌ 收到也静默丢弃 | **双向都无用的死分支** |
| aiops `complete.diagnosis` | ✅（diagnose 包装） | ❌ 只读 `complete.response` | diagnose 的包装对前端是空气，报告内容实际靠 `report` 事件送达 |

结论：**代码行为前后端恰好自洽，坏的是文档**（已修复 ✅：`chat.py`/`aiops.py` 两个 docstring 已改为真实事件集，删掉了 aiops docstring 里跑不起来的 `EventSource` 示例和虚构的 `fetching_alerts`/`target_alert`/`result_preview`/`evidence` 字段；`README.md` 接口表的 `/api/health` 更正为 `/health`；`mcp_servers/README.md` 的 CLS/Monitor 工具清单与诊断示例按实际工具重写）。chat.py 里三个永不触发的翻译分支保留（零成本的前向兼容）。顺带修复了 `file.py:102` 的 `str = None` 类型瑕疵（pyright 警告清零）。

## 4. 前端自身的死代码与瑕疵

- `showLoadingOverlay()`、`formatFileSize()` 定义了没人调；aiops"查看详细步骤"折叠面板所有调用点都传空数组永不渲染；aiops 解析里 `jsonPattern` 正则路径结构性失配；`[DONE]` 哨兵、`currentEvent` 变量是旧协议残留。
- highlight.js 引的是 ES 模块路径却以普通 script 加载，**代码高亮静默失效**（有 `typeof hljs` 兜底所以不报错）。
- `apiBaseUrl` 硬编码 `http://localhost:9900/api`（app.js:4），但 `/api/chat/clear`、`/api/chat/session/{id}` 走相对路径——部署到非 9900 端口时两类请求行为不一致。

## 5. 全系列总结

至此九个模块讲完，项目全貌：

- **主干链路**：FastAPI（薄路由）→ 双引擎（ReAct 对话 / Plan-Execute-Replan 诊断）→ 工具层（本地 3 件 + MCP 3 服务）→ 数据层（Milvus RAG + DashScope LLM + Prometheus）。
- **最突出的工程素养**：全链路的"失败降级不崩溃"（Milvus 降级、MCP 降级、工具错误即数据、LLM 输出兜底）。
- **最典型的 AI 生成痕迹**：死代码（已清理 3 批）、文档吹牛（模型层已修，API 层 docstring 待修）、前后端不一致（上传大小 50MB vs 10MB）。
- **本系列实战修复清单**：模型层对齐（5 死模型 + 2 接口挂 response_model）、死代码清理（llm_factory / vector_search_service / similarity_search）、修剪中间件接线（含泛型类型坑修复）、aiops 状态污染 bug、aiops 进度计数 bug、文档失真修复（chat/aiops docstring 按真实事件重写、README 接口路径、MCP README 工具清单、前后端上传大小对齐为 10MB、file.py 类型标注）。
