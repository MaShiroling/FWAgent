# FireDrill 操作手册（启动 → 验证 → 停止）

> 适用环境：macOS + 本项目当前配置（**Milvus Lite 嵌入式，免 Docker**；演示用 Prometheus 桩）。
> 所有命令在项目根目录执行。本机有代理软件，curl 一律加 `--noproxy '*'`（或确保 `no_proxy` 含 localhost）。

## 0. 首次准备（只做一次）

```bash
# 1. 创建虚拟环境并安装依赖
uv venv && source .venv/bin/activate
uv pip install -e .

# 2. 配置密钥：编辑 .env，必须有两项
#    DASHSCOPE_API_KEY=sk-xxx          （阿里云百炼 API Key，必填，缺了应用 import 阶段就会崩）
#    MILVUS_LITE_PATH=./volumes/milvus_lite.db   （已配置，嵌入式向量库，无需 Docker）
```

⚠️ 本项目用 **Milvus Lite**，所以 **`make init` / `make up` 里的 Docker 步骤不需要执行**（那套是给 Docker 版 Milvus 准备的）。首次启动用下面的流程即可。

## 1. 启动

### 1.1 一键启动（推荐）

```bash
make start        # 依次启动 CLS(8003) → Monitor(8004) → Firewall(8005) → FastAPI(9900)
make wait         # 等待 FastAPI 就绪（最多 60 秒）
```

### 1.2 启动 Prometheus 演示桩（AIOps 诊断需要）

```bash
# 诊断功能的告警来源；没有它诊断会走"诚实失败"路径（报告连不上 Prometheus）
nohup python3 /tmp/prom-demo/prom_stub.py > /tmp/prom-demo/stub.log 2>&1 &
```

> 如果 `/tmp/prom-demo/prom_stub.py` 不存在（比如重启过电脑 /tmp 被清空），从项目文档重建：
> 见本文档附录 A。

### 1.3 首次启动后：上传知识库文档

```bash
make upload       # 把 aiops-docs/*.md 全部上传并向量化（5 个运维手册）
```

只需做一次（数据持久化在 `volumes/milvus_lite.db`）。重传同名文件会自动覆盖更新，不会重复。

### 1.4 单独启停某个服务

```bash
make start-cls / start-monitor / start-firewall / start-api     # 单独启动
make stop-cls  / stop-monitor  / stop-firewall  / stop-api      # 单独停止
make restart                 # 全部重启
make dev                     # 开发模式（前台 + 热重载，改代码自动重启）
```

## 2. 验证（启动后 30 秒自检）

```bash
# ① 主服务健康（期望 200 + milvus connected）
curl -s --noproxy '*' http://localhost:9900/health

# ② MCP 三服务状态（Makefile 自带检查）
make status-mcp

# ③ Prometheus 桩（期望看到 HighCPUUsage / HighMemoryUsage 两条 firing）
curl -s --noproxy '*' http://127.0.0.1:9090/api/v1/alerts | head -c 300

# ④ 防火墙管理通道
curl -s --noproxy '*' http://127.0.0.1:8005/admin/health

# ⑤ 浏览器
open http://localhost:9900        # Web 界面
open http://localhost:9900/docs   # Swagger API 文档
```

端口速查：

| 端口 | 服务 | 用途 |
|---|---|---|
| 9900 | FastAPI 主服务 | Web 界面 + API |
| 8003 | CLS MCP | 日志查询（mock） |
| 8004 | Monitor MCP | 监控指标（mock） |
| 8005 | Firewall MCP | 防火墙演练 + `/admin/*` 管理通道 |
| 9090 | Prometheus 桩 | AIOps 告警来源 |

## 3. 日常使用速查

```bash
# 看日志
tail -f server.log                        # FastAPI（uvicorn 输出）
tail -f logs/app_$(date +%Y-%m-%d).log    # FastAPI（业务日志，Loguru，按天轮转保留7天）
tail -f mcp_cls.log / mcp_monitor.log / mcp_firewall.log   # 三个 MCP 服务

# 对话
curl -s --noproxy '*' -X POST http://localhost:9900/api/chat \
  -H "Content-Type: application/json" -d '{"Id":"test-1","Question":"你好"}'

# AIOps 诊断（流式，每次建议换新 session_id）
curl -sN --noproxy '*' -X POST http://localhost:9900/api/aiops \
  -H "Content-Type: application/json" -d '{"session_id":"diag-'$(date +%s)'"}'

# 防火墙管理通道（评测/演示用，Agent 看不到）
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/reset -H 'Content-Type: application/json' -d '{}'           # 恢复出厂
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/scenario -H 'Content-Type: application/json' \
  -d '{"fault":"commit_lose"}'                                                                                       # 注入故障
curl -s --noproxy '*' http://127.0.0.1:8005/admin/snapshot                                                           # 导出状态快照

# 代码检查
make test          # 27 个测试（防火墙模块）
.tools/uvx pyright app/    # 类型检查
```

## 4. 停止

### 4.1 停止项目服务

```bash
make stop         # 依次停止 FastAPI + CLS + Monitor + Firewall（按 pid 文件 kill）
```

### 4.2 停止演示附加件

```bash
pkill -f prom_stub.py    # 停 Prometheus 桩
```

### 4.3 停止前建议：恢复防火墙出厂状态

```bash
# 如果演示中注入过故障/加过规则，恢复干净状态（演示场景不留残局）
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/reset -H 'Content-Type: application/json' -d '{}'
```

### 4.4 验证全部停止

```bash
lsof -i :9900 -i :8003 -i :8004 -i :8005 -i :9090   # 无输出即全部停止
```

> 说明：会话历史、诊断状态都在内存（MemorySaver），停止即清空，无需额外清理；
> 知识库向量数据在 `volumes/milvus_lite.db`，停止不影响。

## 5. 故障排查

| 现象 | 原因与处理 |
|---|---|
| 启动即报 `请设置环境变量 DASHSCOPE_API_KEY` | `.env` 没配或没生效；embedding 单例 import 期构造，缺 key 直接崩 |
| `DataDirLockedError: another process holds the lock` | Milvus Lite 文件锁：已有主服务进程在跑。`lsof -i :9900` 找到并先停旧的 |
| 对话正常但知识库查不到 | Milvus 未连接（看启动日志 ⚠️ 降级警告）；确认 `MILVUS_LITE_PATH` 配置 |
| 对话里工具不可用 | MCP 服务没起：`make status-mcp` 检查；主服务会降级为仅本地工具 |
| AIOps 报告说连不上 Prometheus | 桩没起：`curl 127.0.0.1:9090/api/v1/alerts` 验证，按 1.2 启动 |
| curl 卡住/超时无响应 | 本机代理劫持了 localhost：curl 加 `--noproxy '*'` |
| 端口被占用 | `lsof -i :<端口>` 找 PID 后 `kill <PID>` |
| 诊断第二次跑结果异常 | 已在代码修复（每次执行前清检查点）；确保主服务是修复后的代码（重启过） |

## 附录 A：Prometheus 桩的重建

`/tmp` 重启后会被清空。桩文件内容：一个返回固定两条 firing 告警的 HTTP 服务，完整代码见 git 历史或按以下要点重建（30 行）：标准库 `http.server`，监听 `127.0.0.1:9090`，`GET /api/v1/alerts` 返回

```json
{"status":"success","data":{"alerts":[
  {"labels":{"alertname":"HighCPUUsage","severity":"critical","instance":"data-sync-service","service":"data-sync-service"},
   "annotations":{"summary":"data-sync-service CPU 使用率持续超过阈值","description":"..."},
   "state":"firing","activeAt":"<25分钟前 RFC3339>","value":"1"},
  {"labels":{"alertname":"HighMemoryUsage","severity":"warning","instance":"data-sync-service","service":"data-sync-service"},
   "annotations":{"summary":"data-sync-service 内存使用率偏高","description":"..."},
   "state":"firing","activeAt":"<10分钟前 RFC3339>","value":"1"}
]}}
```

> 若要长期保留，建议把 `prom_stub.py` 从 /tmp 挪进项目（如 `mcp_servers/prom_stub.py`）并提交 git。

## 附录 B：完整生命周期一览

```
首次:  uv venv → uv pip install -e . → 配 .env
启动:  make start → make wait → (首次) make upload → 启动 prom 桩
验证:  /health + status-mcp + 9090 alerts + 8005 admin/health
使用:  Web 界面 / chat / chat_stream / aiops / upload / 防火墙演练
停止:  (防火墙 reset) → pkill prom_stub → make stop → lsof 确认
```
