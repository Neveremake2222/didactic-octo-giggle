#!/usr/bin/env python3
"""生成中文测试报告。

读取项目已有的测试产物，输出中文 markdown 报告到项目根目录：测试结果.md
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "测试结果.md"


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def read_json(path):
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def score_bar(score, width=20):
    """生成一个 ASCII 进度条。"""
    filled = int(score * width)
    return "[" + "=" * filled + " " * (width - filled) + "]"


def cn_category(cat):
    """英文 category 转中文。"""
    mapping = {
        "documentation": "文档类",
        "text-edit": "文本编辑类",
        "code-fix": "代码修复类",
        "refactoring": "重构类",
        "testing": "测试类",
        "security": "安全类",
        "architecture": "架构类",
        "unknown": "未知类",
    }
    return mapping.get(cat, cat)


def cn_complexity(level):
    mapping = {"low": "低", "medium": "中", "high": "高"}
    return mapping.get(level, level)


def cn_reasoning(cat):
    mapping = {
        "documentation": "文档类任务（低推理需求）",
        "text-edit": "文本编辑（低推理需求）",
        "code-fix": "代码修复（中推理需求）",
        "refactoring": "重构（中高推理需求）",
        "testing": "测试生成（中高推理需求）",
        "security": "安全分析（高推理需求）",
        "architecture": "架构设计（高推理需求）",
        "unknown": "未知任务",
    }
    return mapping.get(cat, cat)


def cn_stop_reason(reason):
    mapping = {
        "final_answer_returned": "返回最终答案",
        "step_limit": "达到步数上限",
        "budget_exhausted": "预算耗尽",
        "approval_denied": "用户拒绝审批",
        "policy_blocked": "策略拦截",
        "model_error": "模型错误",
        "tool_loop": "工具循环",
        "context_overflow": "上下文溢出",
        None: "未知",
    }
    return mapping.get(reason, reason or "未知")


def cn_failure(cat):
    mapping = {
        "verification_failed": "验证失败",
        "wrong_tool_choice": "工具选择错误",
        "repeated_tool_loop": "重复工具调用",
        "budget_exhausted": "预算耗尽",
        "policy_blocked": "策略拦截",
        "context_insufficient": "上下文不足",
        "unknown_failure": "未知原因",
    }
    return mapping.get(cat, cat or "无失败")


# ---------------------------------------------------------------------------
# 第一节：单元测试
# ---------------------------------------------------------------------------

def collect_pytest_summary():
    """运行 pytest 并解析输出，返回摘要。"""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=no", "--color=no"],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=120,
        )
    except Exception as e:
        return {
            "status": "error",
            "message": f"pytest 运行失败: {e}",
            "passed": 0, "failed": 0, "errors": 0, "total": 0, "duration": 0.0,
        }

    output = result.stdout + result.stderr
    lines = output.strip().splitlines()
    summary_line = ""
    for line in reversed(lines):
        if "passed" in line or "failed" in line or "error" in line:
            summary_line = line.strip()
            break

    # 解析 "X passed, Y failed, Z errors in Ws"
    passed = failed = errors = total = 0
    duration = 0.0
    import re
    m = re.search(r"(\d+) passed", summary_line)
    if m: passed = int(m.group(1))
    m = re.search(r"(\d+) failed", summary_line)
    if m: failed = int(m.group(1))
    m = re.search(r"(\d+) error", summary_line)
    if m: errors = int(m.group(1))
    m = re.search(r"in ([\d.]+)s", summary_line)
    if m: duration = float(m.group(1))
    total = passed + failed + errors

    return {
        "status": "ok",
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "total": total,
        "duration": duration,
        "output": summary_line,
    }


def section_unit_tests():
    print("  正在收集单元测试数据 ...")
    result = collect_pytest_summary()
    lines = ["## 一、单元测试概览", ""]

    if result["status"] == "error":
        lines.append(f"> 警告：无法运行 pytest — {result['message']}")
        lines.append("")
        return lines

    r = result
    pass_rate = r["passed"] / r["total"] if r["total"] else 0
    bar = score_bar(pass_rate)

    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 测试总数 | {r['total']} |")
    lines.append(f"| 通过 | {r['passed']} |")
    lines.append(f"| 失败 | {r['failed']} |")
    lines.append(f"| ERROR（环境问题） | {r['errors']} |")
    lines.append(f"| 通过率 | {bar} {pass_rate:.1%} |")
    lines.append(f"| 总耗时 | {r['duration']:.2f}s |")
    lines.append("")
    lines.append(f"**原始输出：** `{r['output']}`")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 第二节：Benchmark 基线测试
# ---------------------------------------------------------------------------

def section_benchmark():
    print("  正在读取 Benchmark 数据 ...")
    benchmark_path = ROOT / "benchmarks" / "benchmark-v1.json"
    data = read_json(benchmark_path)
    lines = ["## 二、Benchmark 基线测试", ""]

    if data is None:
        lines.append("> 暂无 benchmark 数据，请先运行 `run_benchmark.bat` 或 `run_fixed_benchmark()`。")
        lines.append("")
        return lines

    summary = data.get("summary", {})
    rows = data.get("rows", [])
    pass_count = summary.get("passed", 0)
    total_count = summary.get("total_tasks", len(rows))
    pass_rate = summary.get("pass_rate", pass_count / total_count if total_count else 0)

    lines.append(f"**通过率：** {score_bar(pass_rate)} {pass_rate:.1%} "
                  f"（{pass_count}/{total_count} 个任务通过）")
    lines.append("")
    lines.append(f"**运行时间：** {data.get('captured_at', '未知')}")
    lines.append("")

    # 四层评分
    scores = {"outcome": [], "process": [], "efficiency": [], "safety": []}
    for row in rows:
        evals = row.get("evaluations", {})
        for key in scores:
            s = evals.get(key, {}).get("score")
            if s is not None:
                scores[key].append(s)

    lines.append("### 四层评分概览")
    lines.append("")
    lines.append("| 评分维度 | 平均得分 | 说明 |")
    lines.append("|----------|----------|------|")
    labels = {
        "outcome": "结果评分（任务是否成功完成）",
        "process": "过程评分（是否正确使用工具）",
        "efficiency": "效率评分（资源消耗情况）",
        "safety": "安全评分（是否触发安全策略）",
    }
    for key, label in labels.items():
        vals = scores[key]
        avg = sum(vals) / len(vals) if vals else 0.0
        bar = score_bar(avg)
        lines.append(f"| {bar} **{avg:.2f}** | {label} |")
    lines.append("")

    # 按 category 分组
    lines.append("### 各任务详情")
    lines.append("")
    lines.append("| # | 类别 | 复杂度 | 工具步数 | 停止原因 | 失败类型 | 综合分 |")
    lines.append("|---|------|--------|----------|----------|----------|--------|")
    for i, row in enumerate(rows):
        cat = row.get("category", "unknown")
        comp = row.get("complexity", {})
        steps = row.get("tool_steps", "?")
        stop = cn_stop_reason(row.get("stop_reason"))
        failure = cn_failure(row.get("failure_category"))
        overall = row.get("overall_score", 0.0)
        lines.append(
            f"| {i+1} | {cn_category(cat)} | "
            f"推理{cn_complexity(comp.get('reasoning'))} "
            f"工具{cn_complexity(comp.get('tool'))} | "
            f"{steps} | {stop} | {failure} | {overall:.2f} |"
        )
    lines.append("")

    # 按类别汇总
    cats = {}
    for row in rows:
        c = row.get("category", "unknown")
        cats.setdefault(c, []).append(row.get("overall_score", 0.0))

    lines.append("### 按类别得分")
    lines.append("")
    lines.append("| 类别 | 任务数 | 平均综合分 |")
    lines.append("|------|--------|------------|")
    for cat, vals in sorted(cats.items()):
        avg = sum(vals) / len(vals)
        lines.append(f"| {cn_category(cat)} | {len(vals)} | {avg:.3f} |")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 第三节：运行指标汇总（聚合 .owl/runs 下的 metrics）
# ---------------------------------------------------------------------------

def section_run_metrics():
    print("  正在聚合运行指标 ...")
    runs_root = ROOT / ".owl" / "runs"
    lines = ["## 三、运行指标汇总", ""]

    if not runs_root.exists():
        lines.append("> 暂无运行记录（.owl/runs 目录不存在）。")
        lines.append("")
        return lines

    run_dirs = sorted([d for d in runs_root.iterdir() if d.is_dir()], reverse=True)
    if not run_dirs:
        lines.append("> 暂无运行记录。")
        lines.append("")
        return lines

    # 收集所有 metrics
    all_outcome, all_process, all_efficiency, all_safety = [], [], [], []
    for run_dir in run_dirs:
        m = read_json(run_dir / "metrics.json")
        if m:
            if m.get("outcome"): all_outcome.append(m["outcome"])
            if m.get("process"): all_process.append(m["process"])
            if m.get("efficiency"): all_efficiency.append(m["efficiency"])
            if m.get("safety"): all_safety.append(m["safety"])

    def avg(lst, key):
        vals = [x[key] for x in lst if key in x and x[key] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def sum_key(lst, key):
        return sum(x[key] for x in lst if key in x and x[key] is not None)

    lines.append(f"共统计 {len(all_outcome)} 次运行记录（最新 {min(len(run_dirs), 20)} 个 run）")
    lines.append("")

    # 工具使用统计
    lines.append("### 工具使用统计")
    lines.append("")
    if all_process:
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 平均工具调用次数 | {avg(all_process, 'tool_call_count'):.2f} |")
        lines.append(f"| 平均不同工具数 | {avg(all_process, 'unique_tool_count'):.2f} |")
        lines.append(f"| 平均重复调用次数 | {avg(all_process, 'repeated_identical_call_count'):.2f} |")
        lines.append(f"| 平均失败调用次数 | {avg(all_process, 'failed_tool_call_count'):.2f} |")
        lines.append(f"| 零进度循环次数 | {sum_key(all_process, 'no_progress_loop_count'):.0f} |")
    else:
        lines.append("暂无 process 数据。")
    lines.append("")

    # 效率指标
    lines.append("### 效率指标")
    lines.append("")
    if all_efficiency:
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 平均总运行时间 | {avg(all_efficiency, 'total_runtime_ms'):.0f} ms |")
        lines.append(f"| 平均工具耗时 | {avg(all_efficiency, 'avg_tool_ms'):.1f} ms |")
        lines.append(f"| 平均上下文构建次数 | {avg(all_efficiency, 'context_built_count'):.2f} |")
    else:
        lines.append("暂无 efficiency 数据。")
    lines.append("")

    # 安全指标
    lines.append("### 安全指标")
    lines.append("")
    if all_safety:
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 策略拦截次数 | {sum_key(all_safety, 'policy_block_count')} |")
        lines.append(f"| 审批拒绝次数 | {sum_key(all_safety, 'approval_denied_count')} |")
        lines.append(f"| 路径违规次数 | {sum_key(all_safety, 'path_violation_count')} |")
        lines.append(f"| 安全事件次数 | {sum_key(all_safety, 'security_event_count')} |")
    else:
        lines.append("暂无 safety 数据。")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 第四节：核心数据（来自 resume_metrics.json）
# ---------------------------------------------------------------------------

def section_core_metrics():
    print("  正在读取核心指标 ...")
    resume_path = ROOT / "results" / "resume_metrics.json"
    data = read_json(resume_path)
    lines = ["## 四、核心数据", ""]

    if data is None:
        lines.append("> 暂无 resume_metrics.json，请运行 `run_benchmark.bat` 生成。")
        lines.append("")
        return lines

    facts = data.get("facts", {})
    benchmark = data.get("benchmark", {})
    runs = data.get("runs", {})
    mode = data.get("experiment_mode", "synthetic")

    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 实验模式 | {mode} |")
    lines.append(f"| 模型后端数 | {facts.get('model_backend_count', '?')} |")
    lines.append(f"| 工具类型数 | {facts.get('tool_count', '?')} |")
    lines.append(f"| Benchmark 任务数 | {benchmark.get('task_count', '?')} |")
    lines.append(f"| Benchmark 通过率 | {benchmark.get('pass_rate', 0):.2%} |")
    lines.append(f"| 聚合运行次数 | {runs.get('run_count', '?')} |")
    lines.append(f"| 平均工具步数 | {runs.get('avg_tool_steps', 0):.2f} |")
    lines.append(f"| 平均尝试次数 | {runs.get('avg_attempts', 0):.2f} |")
    lines.append(f"| 缓存命中率 | {runs.get('cache_hit_rate', 0):.2%} |")

    # Memory 实验
    memory = data.get("memory_experiment", {})
    if memory:
        on_reads = memory.get("memory_on", {}).get("repeated_reads", "?")
        off_reads = memory.get("memory_off", {}).get("repeated_reads", "?")
        lines.append(f"| 记忆开启后重复读取 | {on_reads} |")
        lines.append(f"| 记忆关闭时重复读取 | {off_reads} |")

    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 第五节：总结与建议
# ---------------------------------------------------------------------------

def section_summary():
    print("  正在生成总结 ...")
    benchmark_path = ROOT / "benchmarks" / "benchmark-v1.json"
    data = read_json(benchmark_path)
    lines = ["## 五、总结与建议", ""]

    if data is None:
        lines.append("暂无数据可总结。")
        lines.append("")
        return lines

    summary = data.get("summary", {})
    pass_rate = summary.get("pass_rate", 0)
    rows = data.get("rows", [])

    # 生成核心结论
    if pass_rate >= 1.0:
        verdict = "🎉 所有 Benchmark 任务全部通过，系统运行稳定。"
    elif pass_rate >= 0.8:
        verdict = "✅ Benchmark 通过率良好，系统整体可用。"
    elif pass_rate >= 0.5:
        verdict = "⚠️ Benchmark 通过率一般，建议关注失败任务。"
    else:
        verdict = "❌ Benchmark 通过率偏低，需要重点排查。"

    lines.append(f"**整体评估：** {verdict}")
    lines.append("")
    lines.append("**主要观察：**")
    lines.append("")

    # 效率观察
    process_scores = [r.get("evaluations", {}).get("process", {}).get("score", 1.0) for r in rows]
    avg_process = sum(process_scores) / len(process_scores) if process_scores else 0
    if avg_process >= 0.9:
        lines.append("- 工具使用过程评分优秀，Agent 工具调用合理。")
    elif avg_process >= 0.7:
        lines.append("- 工具使用过程评分良好，存在少量工具选择或重复调用问题。")
    else:
        lines.append("- 工具使用过程评分偏低，存在较多工具调用问题，建议检查工具定义。")

    # 安全观察
    safety_scores = [r.get("evaluations", {}).get("safety", {}).get("score", 1.0) for r in rows]
    avg_safety = sum(safety_scores) / len(safety_scores) if safety_scores else 0
    if avg_safety >= 0.9:
        lines.append("- 安全策略运行正常，无路径违规或策略拦截。")
    elif avg_safety >= 0.7:
        lines.append("- 安全策略基本正常，偶有拦截事件。")
    else:
        lines.append("- 安全策略触发频繁，请检查是否有误拦截情况。")

    # 失败分类
    failure_counts = data.get("failure_category_counts", {})
    if failure_counts:
        lines.append("")
        lines.append("**失败类型分布：**")
        for cat, count in sorted(failure_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {cn_failure(cat)}: {count} 次")
    else:
        lines.append("- 暂无失败记录。")

    lines.append("")
    lines.append("---")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def main():
    print("=" * 50)
    print("  Owl 测试报告生成器")
    print("=" * 50)
    print()

    sections = []
    sections.extend(section_unit_tests())
    sections.extend(section_benchmark())
    sections.extend(section_run_metrics())
    sections.extend(section_core_metrics())
    sections.extend(section_summary())

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    header = [
        "# Owl 测试结果报告",
        "",
        f"> 生成时间：{now}",
        f"> 项目路径：`{ROOT}`",
        "",
        "---",
        "",
    ]

    report = "\n".join(header) + "\n".join(sections)

    REPORT_PATH.write_text(report, encoding="utf-8")
    print()
    print("=" * 50)
    print(f"✅ 报告已生成：{REPORT_PATH}")
    print("=" * 50)


if __name__ == "__main__":
    raise SystemExit(main())
