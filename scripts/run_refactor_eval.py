#!/usr/bin/env python3
"""Unified refactor evaluation runner (L2 + L3).

Usage:
    python scripts/run_refactor_eval.py \\
        --behavior benchmarks/refactor_behavior_v1.json \\
        --failure benchmarks/refactor_failure_v1.json \\
        --iterations 20 --mode full

Output:
    artifacts/eval/<experiment-name>/
        behavior_summary.json
        failure_summary.json
        memory_report.json
        trace_report.json
        refactor_eval_report_zh.md
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from owl.evaluator import run_fixed_benchmark, summarize_rows, BenchmarkEvaluator
from owl.trace_validator import compute_trace_metrics
from owl.memory_experiments_v2 import run_memory_experiments_v2

DEFAULT_OUTPUT_ROOT = Path("artifacts") / "eval"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _timestamp_slug(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "refactor-eval"


def _safe_pct(value: float) -> str:
    return f"{value:.2%}"


def default_experiment_name(iterations: int, mode: str, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    return f"refactor-{_timestamp_slug(timezone_name)}-{iterations}x-{mode}"


def discover_run_dirs(workspace_root: Path) -> list[Path]:
    run_dirs = []
    for run_dir in sorted(workspace_root.rglob("run_*")):
        if not run_dir.is_dir():
            continue
        if (run_dir / "report.json").exists():
            run_dirs.append(run_dir)
    return run_dirs


def load_trace_for_run(run_dir: Path) -> list[dict[str, Any]]:
    trace_path = run_dir / "trace.jsonl"
    if not trace_path.exists():
        return []
    events = []
    for line in trace_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def summarize_refactor_benchmark(
    benchmark_path: Path,
    output_dir: Path,
    behavior_name: str,
) -> dict[str, Any]:
    """Run a behavioral/failure benchmark and collect results."""
    artifact_path = output_dir / f"benchmark-{behavior_name}.json"
    workspace_root = output_dir / "workspaces" / behavior_name
    if workspace_root.exists():
        shutil.rmtree(workspace_root)
    workspace_root.mkdir(parents=True, exist_ok=True)

    print(f"[benchmark:{behavior_name}] running {benchmark_path}")
    artifact = run_fixed_benchmark(
        benchmark_path=str(benchmark_path),
        artifact_path=str(artifact_path),
        workspace_root=str(workspace_root),
    )
    return artifact


def aggregate_trace_metrics_from_workspace(workspace_root: Path) -> dict[str, Any]:
    """Collect trace events from all runs in a workspace and compute metrics."""
    all_traces: list[list[dict[str, Any]]] = []
    for run_dir in discover_run_dirs(workspace_root):
        events = load_trace_for_run(run_dir)
        if events:
            all_traces.append(events)
    if not all_traces:
        return {
            "trace_completeness_rate": 0.0,
            "trace_order_valid_rate": 0.0,
            "context_built_coverage": 0.0,
            "avg_event_count": 0.0,
            "memory_event_rate": 0.0,
        }
    return compute_trace_metrics(all_traces)


def build_behavior_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build a summary of behavioral benchmark results."""
    rows = artifact.get("rows", [])
    summary = artifact.get("summary", {})

    # Group by tags
    tag_results: dict[str, list[dict]] = {}
    for row in rows:
        tags = row.get("tags", [])
        if not tags:
            tag_results.setdefault("untagged", []).append(row)
        for tag in tags:
            tag_results.setdefault(tag, []).append(row)

    # Process metrics per task
    process_metrics = []
    for row in rows:
        evals = row.get("evaluations", {})
        process = evals.get("process", {})
        metrics = row.get("metrics", {})
        process_metrics.append({
            "task_id": row["id"],
            "repeated_identical_call_count": process.get("repeated_identical_call_count", 0),
            "no_progress_loop_count": process.get("no_progress_loop_count", 0),
            "tool_call_count": metrics.get("process", {}).get("tool_call_count", 0),
        })

    return {
        "task_count": summary.get("total_tasks", len(rows)),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "pass_rate": summary.get("pass_rate", 0.0),
        "by_tag": {
            tag: {
                "count": len(tag_rows),
                "passed": sum(1 for r in tag_rows if r.get("passed")),
                "pass_rate": sum(1 for r in tag_rows if r.get("passed")) / len(tag_rows),
            }
            for tag, tag_rows in tag_results.items()
        },
        "process_metrics": process_metrics,
        "avg_repeated_calls": mean([m["repeated_identical_call_count"] for m in process_metrics]) if process_metrics else 0.0,
    }


def build_failure_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Build a summary of failure-mode benchmark results."""
    rows = artifact.get("rows", [])
    summary = artifact.get("summary", {})

    # Check stop reason hit rate
    stop_reason_hits = []
    for row in rows:
        task_config = {}
        expected_sr = row.get("stop_reason")  # Not directly available; check from task
        # For failure tasks, pass means stop_reason matched expected
        stop_reason_hits.append({
            "task_id": row["id"],
            "passed": row.get("passed", False),
            "stop_reason": row.get("stop_reason", ""),
        })

    total = len(stop_reason_hits)
    hits = sum(1 for r in stop_reason_hits if r["passed"])

    return {
        "task_count": summary.get("total_tasks", total),
        "passed": summary.get("passed", 0),
        "failed": summary.get("failed", 0),
        "pass_rate": summary.get("pass_rate", 0.0),
        "stop_reason_hit_rate": hits / total if total else 0.0,
        "details": stop_reason_hits,
    }


def build_memory_report(memory_results: dict[str, Any]) -> dict[str, Any]:
    """Build memory experiment report."""
    noise = memory_results.get("noise_recall", {})
    conflict = memory_results.get("conflict_resolution", {})
    cross = memory_results.get("cross_session", {})

    return {
        "noise_recall": {
            "clean_correct_rate": noise.get("variants", {}).get("clean", {}).get("correct_rate", 0.0),
            "noisy_correct_rate": noise.get("variants", {}).get("noisy", {}).get("correct_rate", 0.0),
            "memory_off_correct_rate": noise.get("variants", {}).get("memory_off", {}).get("correct_rate", 0.0),
        },
        "conflict_resolution": {
            "correct_recall_rate": conflict.get("correct_recall_rate", 0.0),
            "stale_recall_rate": conflict.get("stale_recall_rate", 0.0),
        },
        "cross_session": {
            "correct_recall_rate": cross.get("correct_recall_rate", 0.0),
            "repeated_reads": cross.get("repeated_reads", 0),
        },
        "summary": {
            "all_noise_recall_above_90pct": (
                noise.get("variants", {}).get("noisy", {}).get("correct_rate", 0.0) >= 0.9
            ),
            "stale_recall_below_5pct": conflict.get("stale_recall_rate", 1.0) < 0.05,
        },
    }


def build_trace_report(behavior_traces: dict[str, Any], failure_traces: dict[str, Any]) -> dict[str, Any]:
    """Build trace quality report."""
    combined = {**behavior_traces, **failure_traces}

    all_complete = all(v.get("trace_completeness_rate", 0) >= 0.99 for v in combined.values())
    all_order_valid = all(v.get("trace_order_valid_rate", 0) >= 0.99 for v in combined.values())

    return {
        "by_benchmark": {
            name: {
                "trace_completeness_rate": v.get("trace_completeness_rate", 0.0),
                "trace_order_valid_rate": v.get("trace_order_valid_rate", 0.0),
                "context_built_coverage": v.get("context_built_coverage", 0.0),
                "memory_event_rate": v.get("memory_event_rate", 0.0),
                "avg_event_count": v.get("avg_event_count", 0.0),
            }
            for name, v in combined.items()
        },
        "summary": {
            "all_traces_complete": all_complete,
            "all_traces_order_valid": all_order_valid,
            "overall_trace_completeness_rate": mean([v.get("trace_completeness_rate", 0) for v in combined.values()]) if combined else 0.0,
            "overall_trace_order_valid_rate": mean([v.get("trace_order_valid_rate", 0) for v in combined.values()]) if combined else 0.0,
        },
    }


ACCEPTANCE_CRITERIA = [
    ("prompt_chars 变异系数", "prompt_chars_cv", "<= 12%"),
    ("irrelevant_recall_rate", "irrelevant_recall_rate", "< 10%"),
    ("repeated_identical_call_count 平均值", "repeated_identical_call_count_avg", "<= 0.2"),
    ("expected_stop_reason 命中率", "expected_stop_reason_hit_rate", ">= 95%"),
    ("stale_recall_rate", "stale_recall_rate", "< 5%"),
    ("trace_completeness_rate", "trace_completeness_rate", "= 100%"),
]


def build_acceptance_table(
    behavior_summary: dict[str, Any],
    failure_summary: dict[str, Any],
    memory_report: dict[str, Any],
    trace_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build acceptance criteria table from collected results."""
    noise_correct = memory_report.get("noise_recall", {}).get("noisy_correct_rate", 0.0)
    irrelevant_rate = 1.0 - noise_correct  # approx
    stale_rate = memory_report.get("conflict_resolution", {}).get("stale_recall_rate", 0.0)
    trace_complete = trace_report.get("summary", {}).get("overall_trace_completeness_rate", 0.0)
    stop_hit = failure_summary.get("stop_reason_hit_rate", 0.0)
    repeated_avg = behavior_summary.get("avg_repeated_calls", 0.0)

    values = {
        "repeated_identical_call_count_avg": repeated_avg,
        "expected_stop_reason_hit_rate": stop_hit,
        "stale_recall_rate": stale_rate,
        "trace_completeness_rate": trace_complete,
    }

    return [
        {
            "目标": name,
            "指标": metric_key,
            "实际值": f"{values.get(metric_key, 0.0) * 100:.2f}%" if "%" in threshold else f"{values.get(metric_key, 0.0):.2f}",
            "门槛": threshold,
            "通过": _check_criterion(metric_key, values.get(metric_key, 0.0), threshold),
        }
        for name, metric_key, threshold in ACCEPTANCE_CRITERIA
    ]


def _check_criterion(key: str, value: float, threshold: str) -> bool:
    """Check if a value meets the acceptance threshold.

    Values are stored as ratios (0.0-1.0) but thresholds use percentages (e.g., 95%).
    When the threshold contains '%', multiply the value by 100 before comparing.
    """
    is_pct = "%" in threshold
    compare_value = value * 100 if is_pct else value
    if ">=" in threshold:
        _, target = threshold.split(">=")
        return compare_value >= float(target.strip().rstrip("%"))
    if "<=" in threshold:
        _, target = threshold.split("<=")
        return compare_value <= float(target.strip().rstrip("%"))
    if "<" in threshold:
        _, target = threshold.split("<")
        return compare_value < float(target.strip().rstrip("%"))
    if "=" in threshold:
        _, target = threshold.split("=")
        return abs(compare_value - float(target.strip().rstrip("%"))) < 0.001
    return False


def render_refactor_report(
    experiment_name: str,
    behavior_summary: dict[str, Any],
    failure_summary: dict[str, Any],
    memory_report: dict[str, Any],
    trace_report: dict[str, Any],
    acceptance_table: list[dict[str, Any]],
    generated_at: str,
) -> str:
    """Render unified Chinese refactor evaluation report."""
    lines = [
        "# Owl 重构评测报告",
        "",
        f"- 生成时间：{generated_at}",
        f"- 实验名称：{experiment_name}",
        "",
        "---",
        "",
        "## 1. Behavioral Benchmark 汇总",
        "",
        f"- 任务数：{behavior_summary.get('task_count', 0)}",
        f"- 通过数：{behavior_summary.get('passed', 0)}",
        f"- 通过率：{_safe_pct(behavior_summary.get('pass_rate', 0.0))}",
        f"- 平均重复调用次数：{behavior_summary.get('avg_repeated_calls', 0.0):.2f}",
        "",
        "### 1.1 按 Tag 分组",
        "",
    ]

    for tag, tag_summary in behavior_summary.get("by_tag", {}).items():
        lines.append(f"- **{tag}**：{tag_summary['passed']}/{tag_summary['count']} 通过（{_safe_pct(tag_summary['pass_rate'])}）")

    lines.extend([
        "",
        "---",
        "",
        "## 2. Failure Benchmark 汇总",
        "",
        f"- 任务数：{failure_summary.get('task_count', 0)}",
        f"- 通过数：{failure_summary.get('passed', 0)}",
        f"- 通过率：{_safe_pct(failure_summary.get('pass_rate', 0.0))}",
        f"- Stop Reason 命中率：{_safe_pct(failure_summary.get('stop_reason_hit_rate', 0.0))}",
        "",
        "### 2.1 各任务详情",
        "",
    ])

    for detail in failure_summary.get("details", []):
        status_icon = "✅" if detail["passed"] else "❌"
        lines.append(f"- {status_icon} `{detail['task_id']}` → stop_reason=`{detail['stop_reason']}`")

    lines.extend([
        "",
        "---",
        "",
        "## 3. Memory 实验汇总",
        "",
        f"- 噪声召回（noisy）正确率：{_safe_pct(memory_report.get('noise_recall', {}).get('noisy_correct_rate', 0.0))}",
        f"- 冲突解决正确率：{_safe_pct(memory_report.get('conflict_resolution', {}).get('correct_recall_rate', 0.0))}",
        f"- 陈旧召回率：{_safe_pct(memory_report.get('conflict_resolution', {}).get('stale_recall_rate', 0.0))}",
        f"- 跨 Session 召回率：{_safe_pct(memory_report.get('cross_session', {}).get('correct_recall_rate', 0.0))}",
        "",
        "---",
        "",
        "## 4. Trace 质量汇总",
        "",
        f"- 总体 trace 完整率：{_safe_pct(trace_report.get('summary', {}).get('overall_trace_completeness_rate', 0.0))}",
        f"- 总体 trace 顺序有效率：{_safe_pct(trace_report.get('summary', {}).get('overall_trace_order_valid_rate', 0.0))}",
        "",
        "---",
        "",
        "## 5. 验收指标",
        "",
        "| 目标 | 指标 | 实际值 | 门槛 | 通过 |",
        "|------|------|--------|------|------|",
    ])

    for row in acceptance_table:
        lines.append(f"| {row['目标']} | {row['指标']} | {row['实际值']} | {row['门槛']} | {'✅' if row['通过'] else '❌'} |")

    lines.extend([
        "",
        "---",
        "",
        "## 6. 结论",
        "",
    ])

    pass_count = sum(1 for r in acceptance_table if r["通过"])
    if pass_count == len(acceptance_table):
        lines.append(f"- ✅ 所有 {len(acceptance_table)} 项验收指标全部通过。")
    else:
        lines.append(f"- ⚠️ {pass_count}/{len(acceptance_table)} 项验收指标通过。")
        failed = [r["目标"] for r in acceptance_table if not r["通过"]]
        if failed:
            lines.append(f"- 未通过项：{', '.join(failed)}。")

    lines.append("")
    return "\n".join(lines)


def run_refactor_evaluation(
    *,
    behavior_path: Path | None = None,
    failure_path: Path | None = None,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    experiment_name: str | None = None,
    iterations: int = 20,
    mode: str = "full",
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    if mode not in {"quick", "full"}:
        raise ValueError("mode must be 'quick' or 'full'")

    if experiment_name is None:
        experiment_name = default_experiment_name(iterations, mode, timezone_name)
    experiment_name = _sanitize_name(experiment_name)

    output_dir = output_root / experiment_name
    output_dir.mkdir(parents=True, exist_ok=True)

    memory_reps = 1 if mode == "quick" else 3
    generated_at = datetime.now(ZoneInfo(timezone_name)).isoformat()

    # ---- L2: Run behavioral benchmark ----
    behavior_artifact: dict[str, Any] = {}
    behavior_summary: dict[str, Any] = {}
    behavior_trace_metrics: dict[str, Any] = {}
    all_behavior_workspaces: list[Path] = []
    if behavior_path:
        for i in range(1, iterations + 1):
            artifact = summarize_refactor_benchmark(
                behavior_path, output_dir / "benchmarks", f"behavior-{i:02d}"
            )
            behavior_artifact = artifact  # keep last artifact
            # Workspace is at: output_dir/benchmarks/workspaces/behavior-{i:02d}
            all_behavior_workspaces.append(output_dir / "benchmarks" / "workspaces" / f"behavior-{i:02d}")
        behavior_summary = build_behavior_summary(behavior_artifact)
        # Aggregate trace metrics across all workspaces
        all_traces: list[list[dict[str, Any]]] = []
        for ws in all_behavior_workspaces:
            all_traces.extend([load_trace_for_run(d) for d in discover_run_dirs(ws)])
        behavior_trace_metrics = compute_trace_metrics(all_traces) if all_traces else {}

    # ---- L2: Run failure benchmark ----
    failure_artifact: dict[str, Any] = {}
    failure_summary: dict[str, Any] = {}
    failure_trace_metrics: dict[str, Any] = {}
    all_failure_workspaces: list[Path] = []
    if failure_path:
        failure_artifact = summarize_refactor_benchmark(
            failure_path, output_dir / "benchmarks", "failure"
        )
        failure_summary = build_failure_summary(failure_artifact)
        # Workspace is at: output_dir/benchmarks/workspaces/failure
        ws = output_dir / "benchmarks" / "workspaces" / "failure"
        all_failure_traces: list[list[dict[str, Any]]] = [load_trace_for_run(d) for d in discover_run_dirs(ws)]
        failure_trace_metrics = compute_trace_metrics(all_failure_traces) if all_failure_traces else {}

    # ---- L3: Run memory experiments ----
    print("[memory] running L3 memory experiments")
    memory_results = run_memory_experiments_v2(repetitions=memory_reps)
    memory_report = build_memory_report(memory_results)

    # ---- L3: Trace report ----
    trace_report = build_trace_report(
        {"behavior": behavior_trace_metrics},
        {"failure": failure_trace_metrics},
    )

    # ---- Build acceptance table ----
    acceptance_table = build_acceptance_table(
        behavior_summary, failure_summary, memory_report, trace_report
    )

    # ---- Render report ----
    report_md = render_refactor_report(
        experiment_name=experiment_name,
        behavior_summary=behavior_summary,
        failure_summary=failure_summary,
        memory_report=memory_report,
        trace_report=trace_report,
        acceptance_table=acceptance_table,
        generated_at=generated_at,
    )

    # ---- Write outputs ----
    (output_dir / "behavior_summary.json").write_text(
        json.dumps(behavior_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "failure_summary.json").write_text(
        json.dumps(failure_summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "memory_report.json").write_text(
        json.dumps(memory_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "trace_report.json").write_text(
        json.dumps(trace_report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "refactor_eval_report_zh.md").write_text(report_md + "\n", encoding="utf-8")

    return {
        "experiment_name": experiment_name,
        "output_dir": str(output_dir),
        "behavior_summary": behavior_summary,
        "failure_summary": failure_summary,
        "memory_report": memory_report,
        "trace_report": trace_report,
        "acceptance_table": acceptance_table,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run L2 behavioral + failure benchmarks and L3 memory/trace experiments."
    )
    parser.add_argument(
        "--behavior",
        default="benchmarks/refactor_behavior_v1.json",
        help="Path to behavioral benchmark JSON",
    )
    parser.add_argument(
        "--failure",
        default="benchmarks/refactor_failure_v1.json",
        help="Path to failure-mode benchmark JSON",
    )
    parser.add_argument(
        "--output-root",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Root directory for experiment outputs",
    )
    parser.add_argument(
        "--experiment-name",
        default=None,
        help="Experiment name (auto-generated if not provided)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=20,
        help="Number of benchmark iterations",
    )
    parser.add_argument(
        "--mode",
        choices=("quick", "full"),
        default="full",
        help="'quick' = 1 repetition for memory experiments; 'full' = 3",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help="Timezone for experiment naming",
    )
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = run_refactor_evaluation(
        behavior_path=Path(args.behavior) if args.behavior else None,
        failure_path=Path(args.failure) if args.failure else None,
        output_root=Path(args.output_root),
        experiment_name=args.experiment_name,
        iterations=args.iterations,
        mode=args.mode,
        timezone_name=args.timezone,
    )
    print(f"[done] experiment: {result['experiment_name']}")
    print(f"[done] report: {result['output_dir']}/refactor_eval_report_zh.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
