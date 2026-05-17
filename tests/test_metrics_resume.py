import json
import sys
import types

from owl import metrics as metrics_module


def test_collect_resume_metrics_includes_memory_v2(tmp_path, monkeypatch):
    artifact = tmp_path / "benchmark.json"
    artifact.write_text(
        json.dumps({
            "summary": {
                "total_tasks": 1,
                "passed": 1,
                "failed": 0,
                "pass_rate": 1.0,
                "within_budget": 1,
                "verifier_passes": 1,
            },
            "rows": [{"category": "smoke", "tool_steps": 1, "attempts": 1}],
        }),
        encoding="utf-8",
    )
    runs_root = tmp_path / "runs"
    runs_root.mkdir()

    monkeypatch.setattr(metrics_module, "build_stress_agent_metrics", lambda: {
        "full": {"prompt_chars": 10},
        "no_context_reduction": {"prompt_chars": 20},
    })
    monkeypatch.setattr(metrics_module, "run_memory_dependency_experiment", lambda repetitions=3: {
        "memory_off": {"repeated_reads": 2},
        "memory_on": {"repeated_reads": 1},
    })
    monkeypatch.setattr(metrics_module, "run_large_scale_memory_experiment", lambda repetitions=5: {
        "task_count": 1,
        "variants": {
            "memory_off": {"repeated_reads": 2},
            "memory_on": {"repeated_reads": 1},
        },
    })
    monkeypatch.setattr(metrics_module, "run_context_stress_matrix", lambda repetitions=5: {"summary": {}})
    monkeypatch.setattr(metrics_module, "run_security_experiment_suite", lambda repetitions=3: {"rows": []})

    fake_memory_v2 = types.ModuleType("owl.memory_experiments_v2")
    fake_memory_v2.run_memory_experiments_v2 = lambda repetitions=3: {"runs": repetitions}
    monkeypatch.setitem(sys.modules, "owl.memory_experiments_v2", fake_memory_v2)

    result = metrics_module.collect_resume_metrics(
        artifact,
        runs_root,
        include_memory_v2=True,
        memory_repetitions=4,
    )

    assert result["memory_v2"] == {"runs": 4}
    assert result["benchmark"]["task_count"] == 1
