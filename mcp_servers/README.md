# MCP Servers

为 AIOps 智能诊断提供日志查询、监控数据工具，以及用于配置变更全链路演练的假防火墙。

## 📚 服务列表

### CLS Server (`cls_server.py`)
**日志查询服务** - 端口 8003

**核心工具：**
- `get_current_timestamp` - 获取当前时间戳
- `get_topic_info_by_name` - 查询日志主题
- `search_log` - 日志搜索
- `search_service_logs` - 服务日志查询（支持级别筛选）
- `analyze_log_pattern` - 日志模式分析

### Monitor Server (`monitor_server.py`)
**监控数据服务** - 端口 8004

**核心工具：**
- `query_cpu_metrics` - CPU 使用率查询
- `query_memory_metrics` - 内存使用查询
- `query_process_list` - 进程列表
- `search_historical_tickets` - 历史工单查询
- `get_service_info` / `list_all_services` - 服务信息

### Firewall Server (`firewall_server.py`)
**有状态假防火墙** - 端口 8005

模拟「读配置 → 下发 → 验证 → 出错处理」全链路，采用两阶段下发语义：
所有写操作落在候选配置（candidate）上，`commit_config` 生效，`discard_candidate` 回滚。

**核心工具：**
- 读配置: `get_firewall_overview` / `list_security_zones` / `list_firewall_rules` / `get_firewall_rule`
- 下发: `add_firewall_rule` / `update_firewall_rule` / `delete_firewall_rule` / `move_firewall_rule` / `commit_config` / `discard_candidate`
- 验证: `get_config_diff` / `test_traffic`（模拟报文首条命中匹配）/ `get_rule_hit_count`

**评测管理通道**（HTTP 路由，不暴露给 Agent）：

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/reset` | 恢复出厂状态（body 可选 `{"keep_fault": true}`） |
| POST | `/admin/scenario` | 注入故障：`{"fault": "none\|commit_reject\|commit_flaky\|commit_lose", "fail_times": N}` |
| GET | `/admin/snapshot` | 导出完整状态（规则、revision、命中计数、操作审计日志），供评测打分 |
| GET | `/admin/health` | 健康检查 |

故障注入说明：
- `commit_reject` - commit 永远被设备拒绝
- `commit_flaky` - 前 N 次 commit 报「设备繁忙」，之后恢复（测重试）
- `commit_lose` - commit 实际生效但返回超时失败（测 Agent 核实状态的能力）

## 🚀 快速开始

### 安装依赖
```bash
pip install fastmcp
```

### 启动服务

**方式一：使用 Makefile（推荐）**
```bash
make mcp-start   # 启动所有 MCP 服务
make mcp-stop    # 停止所有 MCP 服务
make mcp-status  # 查看服务状态
```

**方式二：手动启动**
```bash
python mcp_servers/cls_server.py
python mcp_servers/monitor_server.py
python mcp_servers/firewall_server.py
```

## 💡 使用示例

### AIOps 诊断场景

```
用户: data-sync-service 出现告警，请排查

Agent 自动执行:
1. list_all_services() → 查看所有服务状态
2. get_service_info("data-sync-service") → 获取服务详情
3. query_cpu_metrics("data-sync-service") → CPU 趋势分析
4. search_service_logs("data-sync-service", level="error") → 错误日志
5. analyze_log_pattern("data-sync-service") → 日志模式分析
6. search_historical_tickets(service_name="data-sync-service") → 历史工单
7. 综合分析 → 生成诊断报告和修复建议
```

### 工具参数示例

**查询 CPU 指标：**
```python
query_cpu_metrics(
    service_name="data-sync-service",
    start_time="2024-02-14 02:00:00",
    interval="1m"
)
```

**搜索错误日志：**
```python
search_service_logs(
    service_name="data-sync-service",
    log_level="error",
    keyword="timeout",
    limit=100
)
```

**搜索历史工单：**
```python
search_historical_tickets(
    service_name="data-sync-service",
    issue_type="cpu",
    limit=10
)
```

**防火墙变更全链路（读配置 → 下发 → 验证）：**
```python
list_firewall_rules(source="running")                       # 1. 读现有配置
add_firewall_rule(                                          # 2. 下发到候选配置
    name="allow-office-to-gitlab",
    src_zone="trust", dst_zone="dmz",
    src_addr="10.1.8.0/24", dst_addr="172.16.1.20/32",
    protocol="tcp", dst_port="22", action="allow",
    description="运维通道临时放行"
)
get_config_diff()                                           # 3. 确认改动
commit_config()                                             # 4. 提交生效
test_traffic(src_zone="trust", dst_zone="dmz",              # 5. 模拟报文验证
             src_addr="10.1.8.5", dst_addr="172.16.1.20",
             protocol="tcp", dst_port="22")
```

评测脚本可通过 `POST http://127.0.0.1:8005/admin/reset` 重置状态、
`GET /admin/snapshot` 拉取最终配置与操作审计日志打分。

## 🔧 高级配置

### 接入真实 API

当前返回模拟数据。接入真实 API 步骤：

**腾讯云 CLS：**
```bash
# 安装 SDK
pip install tencentcloud-sdk-python

# 配置环境变量
export TENCENTCLOUD_SECRET_ID="your-id"
export TENCENTCLOUD_SECRET_KEY="your-key"

# 在 cls_server.py 中集成
from tencentcloud.cls.v20201016 import cls_client
```

**其他监控系统：**
- Prometheus
- Grafana
- 云监控（腾讯云/阿里云/AWS）
- 自建监控平台

### 自定义 Mock 数据

修改各 Server 文件中的数据生成逻辑，模拟实际场景。

## 📚 参考资料

- [FastMCP 文档](https://github.com/jlowin/fastmcp)
- [MCP 协议](https://modelcontextprotocol.io/)
- [LangGraph 文档](https://langchain-ai.github.io/langgraph/)
- [主项目 README](../README.md)

---

**注意**: 当前版本返回模拟数据，生产环境需配置真实 API。
