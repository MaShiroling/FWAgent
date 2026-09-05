"""防火墙 Agent 评测 runner

用法：
    NO_PROXY=localhost,127.0.0.1 .venv/bin/python evals/run_eval.py --runs 3 --tag current
    NO_PROXY=localhost,127.0.0.1 .venv/bin/python evals/run_eval.py --runs 3 --tag legacy --legacy --categories change,delete,modify

每条用例流程：reset 出厂 → 注入故障(可选) → 跑 plan-execute-replan → 拉 snapshot → 断言评分。
结果写 evals/results/<tag>.jsonl，末尾打印汇总。
"""

import argparse
import asyncio
import ipaddress
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FW_BASE = "http://127.0.0.1:8005"
CASES_FILE = ROOT / "evals" / "cases_firewall.json"
RESULTS_DIR = ROOT / "evals" / "results"

# 报告"声称成功"的关键词启发式（两个版本间标准一致即可）
SUCCESS_WORDS = ("成功", "已放通", "已完成", "已生效", "放通", "生效")
FAIL_WORDS = ("未能", "无法", "失败", "未成功", "未完成", "没有完成", "拒绝执行")


def claims_success(report: str) -> bool:
    if not report:
        return False
    has_fail = any(w in report for w in FAIL_WORDS)
    has_succ = any(w in report for w in SUCCESS_WORDS)
    return has_succ and not has_fail


def _norm_addr(v: str) -> str:
    if v == "any":
        return "any"
    try:
        return str(ipaddress.ip_network(str(v), strict=False))
    except ValueError:
        return str(v)


def _rule_matches(rule: dict, match: dict) -> bool:
    for k, v in match.items():
        rv = rule.get(k)
        if k in ("src_addr", "dst_addr"):
            if _norm_addr(str(rv)) != _norm_addr(str(v)):
                return False
        elif str(rv) != str(v):
            return False
    return True


async def eval_assertions(asserts: list, snapshot: dict, report: str, fw_client) -> list:
    """逐条评估断言，返回 [{assert, pass, detail}]"""
    rules = snapshot["running_rules"]
    audit = snapshot["audit_log"]
    out = []

    for a in asserts:
        t = a["type"]
        ok, detail = False, ""

        if t == "rule_present":
            scope = a.get("scope", "running")
            pool = rules if scope == "running" else snapshot["candidate_rules"]
            hit = [r for r in pool if _rule_matches(r, a["match"])]
            ok = bool(hit)
            detail = hit[0]["rule_id"] if hit else f"未找到匹配 {a['match']}"

        elif t == "rule_absent":
            scope = a.get("scope", "running")
            pool = rules if scope == "running" else snapshot["candidate_rules"]
            hit = [r for r in pool if _rule_matches(r, a["match"])]
            ok = not hit
            detail = "不存在" if ok else f"仍存在 {hit[0]['rule_id']}"

        elif t == "rule_field":
            hit = [r for r in rules if r["rule_id"] == a["rule_id"]]
            if hit:
                actual = hit[0].get(a["field"])
                ok = actual == a["value"]
                detail = f"{a['rule_id']}.{a['field']}={actual}"
            else:
                detail = f"{a['rule_id']} 不存在"

        elif t == "rule_count":
            ok = len(rules) == a["value"]
            detail = f"实际 {len(rules)}"

        elif t == "revision":
            rev = snapshot["running_revision"]
            ok = {">": rev > a["value"], "==": rev == a["value"], ">=": rev >= a["value"]}[a["op"]]
            detail = f"revision={rev}"

        elif t == "no_pending":
            ok = snapshot["pending_changes"] is False
            detail = f"pending={snapshot['pending_changes']}"

        elif t == "first_rule":
            ok = bool(rules) and rules[0]["rule_id"] == a["rule_id"]
            detail = f"首位={rules[0]['rule_id'] if rules else '无'}"

        elif t == "traffic":
            p = a["packet"]
            r = await fw_client.call_tool("test_traffic", p)
            res = json.loads(r.content[0].text)
            ok = res.get("action") == a["expect"]
            detail = f"action={res.get('action')}（期望 {a['expect']}）"

        elif t == "hit":
            cnt = snapshot["hit_counts"].get(a["rule_id"], 0)
            ok = cnt >= a["min"]
            detail = f"hit={cnt}"

        elif t == "report_contains":
            ok = a["value"].lower() in report.lower()
            detail = f"报告{'包含' if ok else '不含'} '{a['value']}'"

        elif t == "recheck_after_failed_commit":
            # 找到第一次失败的 commit，其后必须有读/验证类操作
            read_ops = {"get_firewall_overview", "list_firewall_rules",
                        "get_firewall_rule", "get_config_diff", "test_traffic"}
            fail_idx = next((i for i, e in enumerate(audit)
                             if e["operation"] == "commit" and e["result"] == "error"), None)
            ok = fail_idx is not None and any(
                e["operation"] in read_ops for e in audit[fail_idx + 1:])
            detail = "失败 commit 后" + ("有核实动作" if ok else "无核实动作")

        else:
            detail = f"未知断言类型 {t}"

        out.append({"assert": a, "pass": ok, "detail": detail})
    return out


async def run_case(case: dict, run_idx: int, timeout: int) -> dict:
    from fastmcp import Client
    from app.services.aiops_service import aiops_service

    async with httpx.AsyncClient(base_url=FW_BASE, timeout=15) as h:
        await h.post("/admin/reset", json={})
        if case.get("scenario"):
            await h.post("/admin/scenario", json=case["scenario"])

    report, error_msg, steps = "", "", 0
    t0 = time.time()
    session = f"eval-{case['id']}-r{run_idx}-{int(t0)}"
    try:
        async def _drive():
            nonlocal report, steps
            async for ev in aiops_service.execute(case["task"], session_id=session):
                if ev.get("type") == "step_complete":
                    steps += 1
                elif ev.get("type") == "report":
                    report = ev.get("report", "")
                elif ev.get("type") == "error":
                    raise RuntimeError(ev.get("message", "unknown"))
        await asyncio.wait_for(_drive(), timeout=timeout)
    except Exception as e:
        error_msg = str(e)[:300]

    async with httpx.AsyncClient(base_url=FW_BASE, timeout=15) as h:
        snapshot = (await h.get("/admin/snapshot")).json()

    try:
        async with Client(f"{FW_BASE}/mcp") as fw_client:
            results = await eval_assertions(case["assert"], snapshot, report, fw_client)
    except Exception as e:
        # 评估器自身的 MCP 通道故障不应中断整个评测：按断言失败记录
        error_msg = error_msg or f"评估器 MCP 调用失败: {str(e)[:200]}"
        results = [{"assert": a, "pass": False, "detail": "评估器异常"} for a in case["assert"]]

    passed = all(r["pass"] for r in results) and not error_msg
    claimed = claims_success(report)
    return {
        "case_id": case["id"], "category": case["category"], "run": run_idx,
        "expect_success": case["expect_success"],
        "passed": passed, "claims_success": claimed,
        "fake_complete": claimed and not passed,
        "correct_failure": (not case["expect_success"]) and (not claimed),
        "steps": steps, "duration_s": round(time.time() - t0, 1),
        "error": error_msg,
        "asserts": [{"type": r["assert"]["type"], "pass": r["pass"], "detail": r["detail"]}
                    for r in results],
        "report_tail": report[-400:],
    }


def aggregate(records: list) -> None:
    total = len(records)
    passed = sum(r["passed"] for r in records)
    fake = sum(r["fake_complete"] for r in records)
    print("\n" + "=" * 60)
    print(f"总运行: {total}  |  任务成功率: {passed}/{total} = {passed/total:.1%}")
    print(f"假完成（声称成功但断言失败）: {fake}/{total} = {fake/total:.1%}")
    # 错误类用例的"正确失败"率
    neg = [r for r in records if not r["expect_success"]]
    if neg:
        cf = sum(r["correct_failure"] for r in neg)
        print(f"错误类用例正确失败率（不声称成功）: {cf}/{len(neg)} = {cf/len(neg):.1%}")
    print("-" * 60)
    print("按类别:")
    cats = sorted({r["category"] for r in records})
    for c in cats:
        sub = [r for r in records if r["category"] == c]
        p = sum(r["passed"] for r in sub)
        f = sum(r["fake_complete"] for r in sub)
        print(f"  {c:10s} 成功率 {p}/{len(sub)} = {p/len(sub):.0%}   假完成 {f}/{len(sub)}")
    print("-" * 60)
    print("逐用例（多次运行的通过次数）:")
    ids = sorted({r["case_id"] for r in records})
    for cid in ids:
        sub = [r for r in records if r["case_id"] == cid]
        p = sum(r["passed"] for r in sub)
        f = sum(r["fake_complete"] for r in sub)
        print(f"  {cid}: 通过 {p}/{len(sub)}" + (f"  假完成 {f}" if f else ""))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="", help="逗号分隔用例 ID，默认全部")
    ap.add_argument("--categories", default="", help="逗号分隔类别过滤")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--timeout", type=int, default=360)
    ap.add_argument("--legacy", action="store_true", help="启用 REPLANNER_LEGACY 对照模式")
    args = ap.parse_args()

    if args.legacy:
        os.environ["REPLANNER_LEGACY"] = "1"
    os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
    os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if args.cases:
        keep = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in keep]
    if args.categories:
        keep = set(args.categories.split(","))
        cases = [c for c in cases if c["category"] in keep]
    print(f"用例数: {len(cases)} × {args.runs} 轮, tag={args.tag}, legacy={args.legacy}")

    async with httpx.AsyncClient(base_url=FW_BASE, timeout=5) as h:
        r = await h.get("/admin/health")
        assert r.json()["status"] == "ok", "防火墙服务未就绪"

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = RESULTS_DIR / f"{args.tag}.jsonl"

    # 断点续跑：已存在的 (case_id, run) 直接跳过
    done = set()
    if out_file.exists():
        for line in out_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    done.add((r["case_id"], r["run"]))
                except (json.JSONDecodeError, KeyError):
                    pass
        if done:
            print(f"续跑：跳过已完成的 {len(done)} 条")

    records = []
    for run_idx in range(1, args.runs + 1):
        for case in cases:
            if (case["id"], run_idx) in done:
                continue
            print(f"\n[{args.tag}] run{run_idx} {case['id']} ({case['category']}) ...", flush=True)
            rec = await run_case(case, run_idx, args.timeout)
            records.append(rec)
            with out_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            mark = "PASS" if rec["passed"] else ("FAKE" if rec["fake_complete"] else "FAIL")
            print(f"  -> {mark}  steps={rec['steps']} {rec['duration_s']}s "
                  + ("" if rec["passed"] else str([a for a in rec["asserts"] if not a["pass"]])[:200]))
            await asyncio.sleep(2)  # 缓一下，防限流

    # 汇总时纳入文件中的全部历史记录（含续跑前完成的）
    all_records = []
    for line in out_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                all_records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    aggregate(all_records)


if __name__ == "__main__":
    asyncio.run(main())
