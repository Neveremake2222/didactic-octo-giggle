import json
from pathlib import Path

from owl.eval_campaign import (
    build_campaign_payload,
    default_experiment_name,
    flatten_run_directories,
    metrics_repetitions,
    render_chinese_report,
    summarize_benchmark_campaign,
)


def _write_artifact(path: Path, *, total_tasks: int, passed: int, attempts: list[int], tool_steps: list[int]) -> Path:
    rows = []
    for index in range(total_tasks):
        rows.append(
            {
                "id": f"task-{index + 1}",
                "passed": index < passed,
                "attempts": attempts[index],
                "tool_steps": tool_steps[index],
            }
        )
    payload = {
        "summary": {
            "total_tasks": total_tasks,
            "passed": passed,
            "failed": total_tasks - passed,
            "pass_rate": passed / total_tasks,
        },
        "rows": rows,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_metrics_repetitions_supports_quick_and_full():
    assert metrics_repetitions("quick") == {
        "memory_repetitions": 1,
        "large_memory_repetitions": 1,
        "context_repetitions": 1,
        "security_repetitions": 1,
    }
    assert metrics_repetitions("full") == {
        "memory_repetitions": 3,
        "large_memory_repetitions": 5,
        "context_repetitions": 5,
        "security_repetitions": 3,
    }


def test_default_experiment_name_contains_iterations_and_mode():
    name = default_experiment_name(20, "full")
    assert "20x-full" in name
    assert name.startswith("eval-")


def test_summarize_benchmark_campaign_aggregates_iteration_stats(tmp_path):
    a1 = _write_artifact(tmp_path / "a1.json", total_tasks=6, passed=6, attempts=[2] * 6, tool_steps=[1] * 6)
    a2 = _write_artifact(tmp_path / "a2.json", total_tasks=6, passed=5, attempts=[3] * 6, tool_steps=[2] * 6)

    summary = summarize_benchmark_campaign([a1, a2])

    assert summary["iteration_count"] == 2
    assert summary["task_count"] == 6
    assert summary["expected_run_count"] == 12
    assert summary["full_pass_iterations"] == 1
    assert summary["all_iterations_fully_passed"] is False
    assert summary["avg_attempts_per_task"] == 2.5
    assert summary["avg_tool_steps_per_task"] == 1.5
    assert summary["failed_iterations"][0]["passed"] == 5


def test_flatten_run_directories_copies_reported_runs(tmp_path):
    source_root = tmp_path / "workspace"
    run_one = source_root / "task-a" / ".owl" / "runs" / "run_1"
    run_two = source_root / "task-b" / ".owl" / "runs" / "run_2"
    for run_dir in (run_one, run_two):
        run_dir.mkdir(parents=True)
        (run_dir / "report.json").write_text("{}", encoding="utf-8")
        (run_dir / "trace.jsonl").write_text("", encoding="utf-8")

    flat_root = tmp_path / "flat-runs"
    copied = flatten_run_directories([run_one, run_two], flat_root, 3)

    assert copied == [
        "iter-03-run-001-run_1",
        "iter-03-run-002-run_2",
    ]
    assert (flat_root / copied[0] / "report.json").exists()
    assert (flat_root / copied[1] / "report.json").exists()


def test_render_chinese_report_includes_key_sections(tmp_path):
    paths = {
        "root": tmp_path,
        "benchmark_artifacts": tmp_path / "benchmark-artifacts",
        "benchmark_workspaces": tmp_path / "benchmark-workspaces",
        "flat_runs": tmp_path / "flat-runs",
        "metrics": tmp_path / "metrics",
        "reports": tmp_path / "reports",
    }
    campaign = build_campaign_payload(
        experiment_name="demo",
        mode="full",
        iterations=20,
        benchmark_path="benchmarks/coding_tasks.json",
        paths=paths,
        benchmark_campaign={
            "iteration_count": 20,
            "task_count": 6,
            "expected_run_count": 120,
            "avg_pass_rate": 1.0,
            "min_pass_rate": 1.0,
            "max_pass_rate": 1.0,
            "avg_passed_tasks": 6.0,
            "avg_failed_tasks": 0.0,
            "avg_attempts_per_task": 2.0,
            "avg_tool_steps_per_task": 1.0,
            "full_pass_iterations": 20,
            "all_iterations_fully_passed": True,
            "failed_iterations": [],
            "artifacts": [],
        },
        metrics={
            "runs": {
                "run_count": 120,
                "avg_attempts": 2.0,
                "avg_tool_steps": 1.0,
                "cache_hit_rate": 0.25,
                "avg_cached_tokens": 128.0,
                "cached_token_ratio": 0.1,
                "avg_prompt_build_duration_ms": 200.0,
                "avg_run_duration_ms": 1500.0,
            }
        },
        flattened_run_dirs=["iter-01-run-001-run_1"],
        timezone_name="Asia/Shanghai",
    )
    metrics = {
        "runs": campaign["runs"],
        "stress_ablation": {
            "full": {"prompt_chars": 5600},
            "no_context_reduction": {"prompt_chars": 6700},
        },
        "context_experiment": {
            "summary": {
                "avg_prompt_compression_ratio": 0.18,
                "max_prompt_compression_ratio": 0.33,
            }
        },
        "memory_experiment": {
            "memory_off": {"repeated_reads": 3},
            "memory_on": {"repeated_reads": 0},
        },
        "memory_large_experiment": {
            "variants": {
                "memory_off": {"repeated_reads": 60},
                "memory_on": {"repeated_reads": 0},
            }
        },
        "security_experiment": {"scenario_count": 10, "runs": 30},
    }

    report = render_chinese_report(campaign, metrics)

    assert report.startswith("# Owl ")
    assert "Cache hit rate" in report
    assert "campaign_summary.json" in report
    assert "resume_metrics.json" in report
