from __future__ import annotations

import argparse
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

from .evaluator import run_fixed_benchmark
from .metrics import collect_resume_metrics, render_resume_metrics_markdown

DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")
DEFAULT_OUTPUT_ROOT = Path("artifacts") / "eval"
DEFAULT_ITERATIONS = 20
DEFAULT_MODE = "full"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _timestamp_slug(timezone_name: str) -> str:
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")


def _sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "eval-campaign"


def default_experiment_name(iterations: int, mode: str, timezone_name: str = DEFAULT_TIMEZONE) -> str:
    return f"eval-{_timestamp_slug(timezone_name)}-{iterations}x-{mode}"


def metrics_repetitions(mode: str) -> dict[str, int]:
    if mode == "quick":
        return {
            "memory_repetitions": 1,
            "large_memory_repetitions": 1,
            "context_repetitions": 1,
            "security_repetitions": 1,
        }
    return {
        "memory_repetitions": 3,
        "large_memory_repetitions": 5,
        "context_repetitions": 5,
        "security_repetitions": 3,
    }


def experiment_paths(output_root: str | Path, experiment_name: str) -> dict[str, Path]:
    root = Path(output_root) / experiment_name
    return {
        "root": root,
        "benchmark_artifacts": root / "benchmark-artifacts",
        "benchmark_workspaces": root / "benchmark-workspaces",
        "flat_runs": root / "flat-runs",
        "metrics": root / "metrics",
        "reports": root / "reports",
    }


def ensure_directories(paths: dict[str, Path]) -> None:
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)


def discover_run_directories(workspace_root: str | Path) -> list[Path]:
    root = Path(workspace_root)
    run_dirs = []
    for run_dir in sorted(root.rglob("run_*")):
        if not run_dir.is_dir():
            continue
        if (run_dir / "report.json").exists():
            run_dirs.append(run_dir)
    return run_dirs


def flatten_run_directories(run_dirs: list[Path], flat_runs_root: str | Path, iteration_index: int) -> list[str]:
    destination_root = Path(flat_runs_root)
    copied_names: list[str] = []
    for run_number, run_dir in enumerate(run_dirs, start=1):
        target_name = f"iter-{iteration_index:02d}-run-{run_number:03d}-{run_dir.name}"
        destination = destination_root / target_name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(run_dir, destination)
        copied_names.append(target_name)
    return copied_names


def load_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def summarize_benchmark_campaign(artifact_paths: list[str | Path]) -> dict[str, Any]:
    artifacts = [load_json(path) for path in artifact_paths]
    pass_rates = [float(artifact.get("summary", {}).get("pass_rate", 0.0)) for artifact in artifacts]
    passed_tasks = [int(artifact.get("summary", {}).get("passed", 0)) for artifact in artifacts]
    failed_tasks = [int(artifact.get("summary", {}).get("failed", 0)) for artifact in artifacts]
    task_counts = [int(artifact.get("summary", {}).get("total_tasks", len(artifact.get("rows", [])) or 0)) for artifact in artifacts]
    avg_attempts = []
    avg_tool_steps = []
    full_pass_iterations = 0
    failures: list[dict[str, Any]] = []

    for artifact_path, artifact in zip(artifact_paths, artifacts):
        rows = list(artifact.get("rows", []))
        row_attempts = [int(row.get("attempts", 0)) for row in rows]
        row_tool_steps = [int(row.get("tool_steps", 0)) for row in rows]
        avg_attempts.append(mean(row_attempts) if row_attempts else 0.0)
        avg_tool_steps.append(mean(row_tool_steps) if row_tool_steps else 0.0)
        summary = artifact.get("summary", {})
        if int(summary.get("passed", 0)) == int(summary.get("total_tasks", len(rows) or 0)):
            full_pass_iterations += 1
        else:
            failures.append(
                {
                    "artifact": str(artifact_path),
                    "passed": int(summary.get("passed", 0)),
                    "total_tasks": int(summary.get("total_tasks", len(rows) or 0)),
                    "pass_rate": float(summary.get("pass_rate", 0.0)),
                }
            )

    task_count = task_counts[0] if task_counts else 0
    return {
        "iteration_count": len(artifact_paths),
        "task_count": task_count,
        "expected_run_count": len(artifact_paths) * task_count,
        "avg_pass_rate": mean(pass_rates) if pass_rates else 0.0,
        "min_pass_rate": min(pass_rates) if pass_rates else 0.0,
        "max_pass_rate": max(pass_rates) if pass_rates else 0.0,
        "avg_passed_tasks": mean(passed_tasks) if passed_tasks else 0.0,
        "avg_failed_tasks": mean(failed_tasks) if failed_tasks else 0.0,
        "avg_attempts_per_task": mean(avg_attempts) if avg_attempts else 0.0,
        "avg_tool_steps_per_task": mean(avg_tool_steps) if avg_tool_steps else 0.0,
        "full_pass_iterations": full_pass_iterations,
        "all_iterations_fully_passed": full_pass_iterations == len(artifact_paths),
        "failed_iterations": failures,
        "artifacts": [str(Path(path)) for path in artifact_paths],
    }


def _safe_pct(value: float) -> str:
    return f"{value:.2%}"


def _safe_ms(value: float) -> str:
    return f"{value:.1f} ms"


def _safe_path(path: str | Path) -> str:
    return str(Path(path))


def build_campaign_payload(
    *,
    experiment_name: str,
    mode: str,
    iterations: int,
    benchmark_path: str | Path,
    paths: dict[str, Path],
    benchmark_campaign: dict[str, Any],
    metrics: dict[str, Any],
    flattened_run_dirs: list[str],
    timezone_name: str,
) -> dict[str, Any]:
    generated_at = datetime.now(ZoneInfo(timezone_name)).isoformat()
    return {
        "generated_at": generated_at,
        "experiment_name": experiment_name,
        "mode": mode,
        "iterations": iterations,
        "benchmark_path": _safe_path(benchmark_path),
        "paths": {name: _safe_path(path) for name, path in paths.items()},
        "benchmark_campaign": benchmark_campaign,
        "flattened_run_dir_count": len(flattened_run_dirs),
        "flattened_run_dirs": flattened_run_dirs,
        "runs": metrics.get("runs", {}),
        "metrics_files": {
            "resume_metrics_json": _safe_path(paths["metrics"] / "resume_metrics.json"),
            "resume_metrics_markdown": _safe_path(paths["metrics"] / "resume_metrics.md"),
            "campaign_summary_json": _safe_path(paths["metrics"] / "campaign_summary.json"),
            "report_markdown_zh": _safe_path(paths["reports"] / "eval_report_zh.md"),
        },
    }


def render_chinese_report(campaign: dict[str, Any], metrics: dict[str, Any]) -> str:
    benchmark_campaign = campaign["benchmark_campaign"]
    runs = metrics.get("runs", {})
    stress_ablation = metrics.get("stress_ablation", {})
    context_summary = metrics.get("context_experiment", {}).get("summary", {})
    memory_small = metrics.get("memory_experiment", {})
    memory_large = metrics.get("memory_large_experiment", {}).get("variants", {})
    security = metrics.get("security_experiment", {})
    memory_v2 = metrics.get("memory_v2", {})
    behavior_summary = campaign.get("l2_behavior_summary", {})
    failure_summary = campaign.get("l2_failure_summary", {})

    actual_run_count = int(runs.get("run_count", 0))
    expected_run_count = int(benchmark_campaign.get("expected_run_count", 0))
    run_count_note = "匹配" if actual_run_count == expected_run_count else "不匹配"

    lines = [
        "# Owl 自动化评测中文报告",
        "",
        f"- 生成时间：{campaign['generated_at']}",
        f"- 实验名称：{campaign['experiment_name']}",
        f"- 评测模式：{campaign['mode']}",
        f"- Benchmark 轮数：{campaign['iterations']}",
        f"- Benchmark 配置：`{campaign['benchmark_path']}`",
        "",
        "## 1. 实验目录",
        "",
        f"- 根目录：`{campaign['paths']['root']}`",
        f"- Benchmark 产物：`{campaign['paths']['benchmark_artifacts']}`",
        f"- 扁平化 runs：`{campaign['paths']['flat_runs']}`",
        f"- Metrics 输出：`{campaign['metrics_files']['resume_metrics_json']}`",
        f"- 中文报告：`{campaign['metrics_files']['report_markdown_zh']}`",
        "",
        "## 2. Benchmark 汇总",
        "",
        f"- 平均通过率：{_safe_pct(float(benchmark_campaign.get('avg_pass_rate', 0.0)))}",
        f"- 最低通过率：{_safe_pct(float(benchmark_campaign.get('min_pass_rate', 0.0)))}",
        f"- 最高通过率：{_safe_pct(float(benchmark_campaign.get('max_pass_rate', 0.0)))}",
        f"- 全通过轮数：{benchmark_campaign.get('full_pass_iterations', 0)} / {benchmark_campaign.get('iteration_count', 0)}",
        f"- 平均每任务尝试次数：{float(benchmark_campaign.get('avg_attempts_per_task', 0.0)):.2f}",
        f"- 平均每任务工具步数：{float(benchmark_campaign.get('avg_tool_steps_per_task', 0.0)):.2f}",
        "",
        "## 3. 运行统计",
        "",
        f"- 期望 run 数：{expected_run_count}",
        f"- 实际聚合 run 数：{actual_run_count}",
        f"- run 数校验：{run_count_note}",
        f"- 平均每 run 尝试次数：{float(runs.get('avg_attempts', 0.0)):.2f}",
        f"- 平均每 run 工具步数：{float(runs.get('avg_tool_steps', 0.0)):.2f}",
        f"- Cache hit rate：{_safe_pct(float(runs.get('cache_hit_rate', 0.0)))}",
        f"- 平均 cached tokens：{float(runs.get('avg_cached_tokens', 0.0)):.1f}",
        f"- Cached token ratio：{_safe_pct(float(runs.get('cached_token_ratio', 0.0)))}",
        f"- 平均 prompt 构建耗时：{_safe_ms(float(runs.get('avg_prompt_build_duration_ms', 0.0)))}",
        f"- 平均单次 run 耗时：{_safe_ms(float(runs.get('avg_run_duration_ms', 0.0)))}",
        "",
        "## 4. 受控实验结果",
        "",
    ]

    if stress_ablation:
        full_prompt = int(stress_ablation.get("full", {}).get("prompt_chars", 0))
        reduced_prompt = int(stress_ablation.get("no_context_reduction", {}).get("prompt_chars", 0))
        lines.extend(
            [
                f"- 长上下文压缩：full={full_prompt} chars, no_context_reduction={reduced_prompt} chars",
                f"- 平均压缩率：{_safe_pct(float(context_summary.get('avg_prompt_compression_ratio', 0.0)))}",
                f"- 最高压缩率：{_safe_pct(float(context_summary.get('max_prompt_compression_ratio', 0.0)))}",
            ]
        )

    if memory_small:
        lines.append(
            f"- 小规模 memory 复读对比：memory_off={memory_small.get('memory_off', {}).get('repeated_reads', 0)}, "
            f"memory_on={memory_small.get('memory_on', {}).get('repeated_reads', 0)}"
        )

    if memory_large:
        lines.append(
            f"- 大规模 memory 复读对比：memory_off={memory_large.get('memory_off', {}).get('repeated_reads', 0)}, "
            f"memory_on={memory_large.get('memory_on', {}).get('repeated_reads', 0)}"
        )

    if security:
        lines.append(
            f"- 安全实验：scenarios={security.get('scenario_count', 0)}, runs={security.get('runs', 0)}"
        )

    # ---- L2 Behavioral Benchmark Summary ----
    if behavior_summary:
        lines.extend([
            "",
            "## 5. L2 Behavioral Benchmark",
            "",
            f"- 任务数：{behavior_summary.get('task_count', 0)}",
            f"- 通过率：{_safe_pct(behavior_summary.get('pass_rate', 0.0))}",
            f"- 平均重复调用次数：{behavior_summary.get('avg_repeated_calls', 0.0):.2f}",
            "",
        ])
        for tag, tag_summary in behavior_summary.get("by_tag", {}).items():
            lines.append(
                f"- {tag}：{tag_summary['passed']}/{tag_summary['count']} "
                f"（{_safe_pct(tag_summary['pass_rate'])}）"
            )

    # ---- L2 Failure Benchmark Summary ----
    if failure_summary:
        lines.extend([
            "",
            "## 6. L2 Failure Benchmark",
            "",
            f"- 任务数：{failure_summary.get('task_count', 0)}",
            f"- 通过率：{_safe_pct(failure_summary.get('pass_rate', 0.0))}",
            f"- Stop Reason 命中率：{_safe_pct(failure_summary.get('stop_reason_hit_rate', 0.0))}",
        ])

    # ---- L3 Memory Experiments ----
    if memory_v2:
        noise = memory_v2.get("noise_recall", {}).get("variants", {})
        conflict = memory_v2.get("conflict_resolution", {})
        cross = memory_v2.get("cross_session", {})
        lines.extend([
            "",
            "## 7. L3 Memory 实验",
            "",
            f"- 噪声召回（noisy）正确率：{_safe_pct(noise.get('noisy', {}).get('correct_rate', 0.0))}",
            f"- 冲突解决正确率：{_safe_pct(conflict.get('correct_recall_rate', 0.0))}",
            f"- 陈旧召回率：{_safe_pct(conflict.get('stale_recall_rate', 0.0))}",
            f"- 跨 Session 召回率：{_safe_pct(cross.get('correct_recall_rate', 0.0))}",
        ])

    lines.extend([
        "",
        "## 8. 结论",
        "",
    ])

    if benchmark_campaign.get("all_iterations_fully_passed"):
        lines.append("- 这批重复 benchmark 在功能正确性上稳定，所有轮次都保持全通过。")
    else:
        lines.append("- 这批重复 benchmark 存在未全通过轮次，需优先检查失败轮次对应的 artifact。")

    if actual_run_count != expected_run_count:
        lines.append("- 聚合 run 数与理论值不一致，说明统计目录里有缺失 run 或扫描口径不一致，这会影响运行时指标解释。")
    else:
        lines.append("- 聚合 run 数与理论值一致，本次 metrics 汇总口径完整。")

    if benchmark_campaign.get("iteration_count", 0) < 20:
        lines.append("- 当前轮数低于 20，适合做烟雾验证，不适合做强结论。")
    else:
        lines.append("- 当前轮数达到 20 轮，适合用于对比修复前后趋势。")

    lines.extend(
        [
            "",
            "## 9. 关键输出文件",
            "",
            f"- campaign_summary.json：`{campaign['metrics_files']['campaign_summary_json']}`",
            f"- resume_metrics.json：`{campaign['metrics_files']['resume_metrics_json']}`",
            f"- resume_metrics.md：`{campaign['metrics_files']['resume_metrics_markdown']}`",
            f"- 中文报告：`{campaign['metrics_files']['report_markdown_zh']}`",
        ]
    )

    return "\n".join(lines) + "\n"


def run_evaluation_campaign(
    *,
    benchmark_path: str | Path = DEFAULT_BENCHMARK_PATH,
    output_root: str | Path = DEFAULT_OUTPUT_ROOT,
    experiment_name: str | None = None,
    iterations: int = DEFAULT_ITERATIONS,
    mode: str = DEFAULT_MODE,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be >= 1")
    if mode not in {"quick", "full"}:
        raise ValueError("mode must be 'quick' or 'full'")

    benchmark_path = Path(benchmark_path)
    if experiment_name is None:
        experiment_name = default_experiment_name(iterations, mode, timezone_name)
    experiment_name = _sanitize_name(experiment_name)

    paths = experiment_paths(output_root, experiment_name)
    ensure_directories(paths)

    artifact_paths: list[Path] = []
    flattened_run_dirs: list[str] = []

    for iteration in range(1, iterations + 1):
        artifact_path = paths["benchmark_artifacts"] / f"benchmark-{iteration:02d}.json"
        workspace_root = paths["benchmark_workspaces"] / f"run-{iteration:02d}"
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        print(f"[benchmark] iteration {iteration}/{iterations}")
        run_fixed_benchmark(
            benchmark_path=benchmark_path,
            artifact_path=artifact_path,
            workspace_root=workspace_root,
        )
        artifact_paths.append(artifact_path)
        run_dirs = discover_run_directories(workspace_root)
        flattened_run_dirs.extend(flatten_run_directories(run_dirs, paths["flat_runs"], iteration))

    benchmark_campaign = summarize_benchmark_campaign(artifact_paths)
    repetition_config = metrics_repetitions(mode)
    print("[metrics] collecting resume metrics")
    metrics = collect_resume_metrics(
        artifact_paths[-1],
        paths["flat_runs"],
        experiment_mode="synthetic",
        **repetition_config,
    )

    resume_metrics_json = paths["metrics"] / "resume_metrics.json"
    resume_metrics_md = paths["metrics"] / "resume_metrics.md"
    resume_metrics_json.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    resume_metrics_md.write_text(render_resume_metrics_markdown(metrics) + "\n", encoding="utf-8")

    campaign = build_campaign_payload(
        experiment_name=experiment_name,
        mode=mode,
        iterations=iterations,
        benchmark_path=benchmark_path,
        paths=paths,
        benchmark_campaign=benchmark_campaign,
        metrics=metrics,
        flattened_run_dirs=flattened_run_dirs,
        timezone_name=timezone_name,
    )
    campaign_summary_json = paths["metrics"] / "campaign_summary.json"
    campaign_summary_json.write_text(json.dumps(campaign, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    report_path = paths["reports"] / "eval_report_zh.md"
    report_path.write_text(render_chinese_report(campaign, metrics), encoding="utf-8")
    return campaign


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run repeated benchmark evaluation and write a Chinese report.")
    parser.add_argument("--benchmark-path", default=str(DEFAULT_BENCHMARK_PATH))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--experiment-name", default=None)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--mode", choices=("quick", "full"), default=DEFAULT_MODE)
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    return parser


def cli_main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    campaign = run_evaluation_campaign(
        benchmark_path=args.benchmark_path,
        output_root=args.output_root,
        experiment_name=args.experiment_name,
        iterations=args.iterations,
        mode=args.mode,
        timezone_name=args.timezone,
    )
    print("[done] experiment root:", campaign["paths"]["root"])
    print("[done] chinese report:", campaign["metrics_files"]["report_markdown_zh"])
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
