# 面试 5 分钟演示脚本（界面版）

> 演示主战场是 Web 界面（http://localhost:9900），终端只在注入故障时用。
> 配套：`project-walkthrough.md`（模块详解）、`interview-prep.md`（问答）、`operations-manual.md`（启停）。

## 演示前检查单（T-10 分钟，终端）

```bash
# 1. 确认服务在跑（缺则 make start）
lsof -i :9900 -i :8003 -i :8004 -i :8005

# 2. 启动 Prometheus 演示桩（AIOps 的告警来源）
nohup python3 /tmp/prom-demo/prom_stub.py > /tmp/prom-demo/stub.log 2>&1 &
curl -s --noproxy '*' http://127.0.0.1:9090/api/v1/alerts | head -c 200   # 应见两条 firing 告警

# 3. 防火墙恢复出厂（清掉上次演示的故障和改动）
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/reset -H 'Content-Type: application/json' -d '{}'

# 4. 浏览器打开 http://localhost:9900，点左侧"新建对话"
# 5. 备好一个终端标签页（第二幕注入故障用），提前粘贴好命令
```

## 第一幕：对话与知识库（约 1.5 分钟，全界面）

**操作 1**：输入框右下角确认模式为「流式」，输入：`现在几点了？`

> 讲解词："注意逐字输出——这是 SSE 流式。这个问题触发了 Agent 自主调用时间工具。架构上不是固定流水线，是 LLM 自主决策调工具的 ReAct Agent。"

**操作 2**：点输入框左侧 **"..."** → "上传文件"，选 `aiops-docs/cpu_high_usage.md`

> 讲解词："上传即索引：先删旧数据（覆盖更新）、Markdown 标题感知切分、向量化入 Milvus。"
> （可选加分：切终端 `tail -f logs/app_$(date +%Y-%m-%d).log` 让面试官亲眼看到"删旧 3 条→切 3 片→入库"）

**操作 3**：问 `CPU 使用率过高应该怎么排查和处理？`

> 讲解词："这个答案融合了两个数据源：知识库里的运维手册 + Monitor MCP 服务的实时 CPU 指标，Agent 自己决定两个都调。后端日志能看到 MCP 工具调用记录。"
> （可选：`tail -f mcp_monitor.log` 展示工具调用流水）

## 第二幕：AIOps 自动诊断（约 2 分钟，界面）

**操作**：点主区右上角 **"AI Ops"** 按钮，等待诊断（约 1-2 分钟）。

> 流式过程中讲解："这是 Plan-Execute-Replan 架构，界面上每行是一个阶段：📋 计划已制定（注意计划引用了知识库手册——RAG 参与诊断）→ ✅ 逐步执行（进度计数 N/M）→ 评估决策 → 出报告。Replanner 有 8 步硬顶、5 步禁 replan 的防失控护栏。"

**预期输出**：结束后渲染成 Markdown 报告——告警清单表（HighCPUUsage critical / HighMemoryUsage warning，目标服务 data-sync-service）→ 根因分析 → 处理方案 → 风险评估。

> 收尾讲解："报告格式是 prompt 内嵌模板约束的，且要求'严禁编造、失败如实说明'。我实际测过 Prometheus 挂掉的场景——它会诚实报告连不上，而不是编一份告警。"

⚠️ 预期管理：流式期间是纯文本逐行追加，**结束后才整体渲染 Markdown**，不是打字机。

## 第三幕：防火墙变更演练（约 1.5 分钟，高潮）

**操作 1（正常链路，界面对话）**：

```
请在防火墙上添加一条规则：允许 trust 区域的 10.1.8.0/24 网段访问 dmz 区域
172.16.1.20 的 TCP 22 端口（SSH），规则名 allow-office-ssh。添加后请提交生效，
并模拟从 10.1.8.5 访问 172.16.1.20:22 验证是否放行。
```

> 讲解词："这是一台模拟真实防火墙语义的有状态假设备：两阶段提交——改动先落候选配置，commit 才生效。Agent 刚完成了'改配置→提交→模拟报文验证'的完整运维闭环。"

**操作 2（切终端，注入故障）**：

```bash
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/scenario \
  -H 'Content-Type: application/json' -d '{"fault":"commit_lose"}'
```

> 讲解词（切终端时说）："现在我走一条 Agent 看不到的管理通道，注入最刁钻的故障：commit 实际生效，但设备返回超时失败。这考察 Agent 面对'结果歧义'的判断力。"

**操作 3（切回界面）**：

请在防火墙上添加一条规则：允许 trust 区域的 10.1.8.0/24 网段访问 dmz 区域

172.16.1.21 的 TCP 80 端口（HTTP），规则名 allow-office-http。添加后请提交生效，

并确认配置真的生效了。

**预期**：Agent 报告"提交超时"，但主动用 `get_config_diff` 核实，得出"实际已生效"的结论。

> 收尾讲解："设备说失败了，Agent 没盲从也没谎报，而是用只读接口核实了真实状态。这个故障注入 + `/admin/snapshot` 审计通道，本质是一个 Agent 能力的自动化评测环境。"

## 收尾话术（30 秒）

"代码是我用 AI 辅助搭建的，但我做了一轮完整深度审计：清理死代码、修复了包括会话状态污染、流式进度计数在内的 5 个真实 bug、统一前后端契约，全部有文档和验证记录（`docs/project-walkthrough.md`）。用 AI 不丢人，关键是能审计、能兜底、能对每一行代码负责。"

## 演示后清理（终端）

```bash
pkill -f prom_stub.py    # 停 Prometheus 桩
curl -s --noproxy '*' -X POST http://127.0.0.1:8005/admin/reset -H 'Content-Type: application/json' -d '{}'   # 防火墙复位
# make stop             # 收工全停
```

## 应急备案

| 意外 | 应对 |
|---|---|
| DashScope API 挂了/超时 | 展示日志 + walkthrough 文档讲架构，别硬等 |
| Milvus 连不上 | 现场演示降级设计："看，知识库降级，对话照常" |
| MCP 服务挂了 | 同理："自动降级为仅本地工具"，日志里有警告 |
| 诊断报告质量波动 | LLM 有随机性；提前跑一次留截图/录屏备用 |
| 界面样式异常 | 强制刷新（Cmd+Shift+R）；静态资源由 FastAPI 直接托管 |
| 代码块无语法高亮 | 已知问题（highlight.js 引入方式），别说"看高亮" |

---

# 附：防火墙演练实测记录（2026-08-31 排练）

> 第三幕的两次真实执行记录，含踩坑。演示前读一遍，校准预期。

## 踩坑：排练前没复位防火墙

第一次排练直接发操作 1，Agent 回复"规则 rule-007 已存在于候选配置，未重复添加；提交无改动；流量测试命中现有规则放行"。原因：防火墙是**内存态有状态服务**，上一次演示加的规则一直在，Agent 走进了"幂等防重复"分支——`commit_lose` 故障根本没被触发。

**教训（已写入检查单第 3 步）：每次演示/排练前必须 `POST /admin/reset` 恢复出厂。** 顺带说明：这个"规则已存在不重复添加"的分支本身也是 Agent 健壮性的体现，面试官深挖时可以提。

## 实测一：正常变更链路（操作 1）

输入 SSH 规则请求后，Agent 依次完成：`add_firewall_rule` → `commit_config` → `test_traffic` 验证。

- 规则添加为 `rule-007`，提交生效（revision 1 → 2）
- 模拟 10.1.8.5 → 172.16.1.20:22，命中 rule-007，action allow
- Agent 回复："规则已经生效并且正确地放行了指定的 SSH 流量"

## 实测二：`commit_lose` 故障注入（操作 2 + 3）

终端注入 `{"fault":"commit_lose"}`（返回 `{"success":true,"fault":{"mode":"commit_lose",...}}`）后，发 HTTP 规则请求：

1. 规则添加成功（rule-008）
2. commit 返回"提交超时：设备未返回确认，配置状态未知"——**假失败**
3. Agent 没有盲从失败返回，主动用 `test_traffic` 模拟报文核实：命中 rule-008，allow
4. Agent 结论："提交操作虽然显示失败，但实际上可能已经应用了更改"

**审计日志（地面真相，Agent 看不到）对照**：

```
add_rule     success  新增候选规则 rule-007      ← 操作 1
commit       success  生效，running_revision=2
test_traffic success  命中 rule-007 -> allow
add_rule     success  新增候选规则 rule-008      ← 操作 3
commit       error    提交超时（fault: commit_lose）；实际已生效，revision=3  ← "设备撒谎"现场
test_traffic success  命中 rule-008 -> allow     ← Agent 自己核实到的证据
```

Agent 的结论与审计日志完全一致。

## 概念解释："设备撒谎"是什么

`commit_lose` = **事情办了，嘴上说没办**。设备实际已生效（revision +1），但返回"提交超时、状态未知"。

- Agent 听到的：commit 失败
- 实际发生的：配置已生效（只有评测方通过 `/admin/snapshot` 可见）

这在真实网络中常见（请求到了但回包丢了），异步网络上"结果歧义"无法从单次调用消除（两将军问题）。三种应对只有核实是合格的：

- ❌ 盲信失败 → 谎报"没改成"
- ❌ 无脑重试 commit → 真实系统可能重复下发、配置冲突
- ✅ 用只读接口（test_traffic / get_config_diff / get_firewall_overview）交叉验证真实状态

## 两次核实路径对比（同一故障，Agent 两次选择了不同工具）

| 执行 | 核实工具 | 证据类型 |
|---|---|---|
| 2026-08-30（开发者跑） | `get_config_diff` | 配置状态：候选=运行，无未提交改动 → 推断已生效 |
| 2026-08-31（排练） | `test_traffic` | 行为证据：真实报文命中新规则 → 直接证明已生效 |

两条路径都正确；`test_traffic` 验证的是**行为**而非配置，证据更强。LLM 有随机性，演示时两种回答都属正常，讲解词兼容两种即可（"Agent 会主动核实，可能查配置差异，也可能直接模拟报文验证"）。
