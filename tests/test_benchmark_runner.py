"""benchmark_runner 测试。"""

import json
import pytest
from pathlib import Path

from owl.benchmark_runner import BenchmarkResult, BenchmarkRunner, ComparisonReport


def _write_artifact(tmp_path, name, pass_rate, rows):
    artifact = {
        "schema_version": 1,
        "captured_at": "2026-04-16T00:00:00+08:00",
        "runtime": {"commit_sha": "abc123", "branch": "main"},
        "summary": {
            "total_tasks": len(rows),
            "passed": sum(1 for r in rows if r.get("passed")),
            "failed": sum(1 for r in rows if not r.get("passed")),
            "pass_rate": pass_rate,
        },
        "rows": rows,
    }
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(artifact) + "\n", encoding="utf-8")
    return path


def _make_rows(tasks):
    return [{"id": tid, "passed": p, "category": cat} for tid, p, cat in tasks]


class TestBenchmarkResult:
    def test_load_and_summary(self, tmp_path):
        rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        path = _write_artifact(tmp_path, "result.json", 0.5, rows)
        result = BenchmarkResult(path)
        assert result.task_count == 2
        assert result.pass_rate == 0.5
        assert len(result.rows) == 2

    def test_per_category(self, tmp_path):
        rows = _make_rows([("t1", True, "docs"), ("t2", True, "docs"), ("t3", False, "edit")])
        path = _write_artifact(tmp_path, "result.json", 0.667, rows)
        result = BenchmarkResult(path)
        cat_rates = result.per_category_pass_rate()
        assert cat_rates["docs"] == 1.0
        assert cat_rates["edit"] == 0.0


class TestComparisonReport:
    def test_identical_results(self, tmp_path):
        rows = _make_rows([("t1", True, "docs"), ("t2", True, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 1.0, rows)
        cp = _write_artifact(tmp_path, "candidate.json", 1.0, rows)
        report = ComparisonReport(BenchmarkResult(bp), BenchmarkResult(cp)).run()
        assert report["overall_delta"] == 0.0
        assert report["regression_count"] == 0
        assert report["improvement_count"] == 0

    def test_improvement(self, tmp_path):
        b_rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        c_rows = _make_rows([("t1", True, "docs"), ("t2", True, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 0.5, b_rows)
        cp = _write_artifact(tmp_path, "candidate.json", 1.0, c_rows)
        report = ComparisonReport(BenchmarkResult(bp), BenchmarkResult(cp)).run()
        assert report["overall_delta"] == 0.5
        assert report["improvement_count"] == 1
        assert report["regression_count"] == 0

    def test_regression(self, tmp_path):
        b_rows = _make_rows([("t1", True, "docs"), ("t2", True, "edit")])
        c_rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 1.0, b_rows)
        cp = _write_artifact(tmp_path, "candidate.json", 0.5, c_rows)
        report = ComparisonReport(BenchmarkResult(bp), BenchmarkResult(cp)).run()
        assert report["overall_delta"] == -0.5
        assert report["regression_count"] == 1
        assert report["improvement_count"] == 0

    def test_per_category_delta(self, tmp_path):
        b_rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        c_rows = _make_rows([("t1", False, "docs"), ("t2", True, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 0.5, b_rows)
        cp = _write_artifact(tmp_path, "candidate.json", 0.5, c_rows)
        report = ComparisonReport(BenchmarkResult(bp), BenchmarkResult(cp)).run()
        assert report["per_category_delta"]["docs"]["delta"] == -1.0
        assert report["per_category_delta"]["edit"]["delta"] == 1.0

    def test_task_deltas(self, tmp_path):
        b_rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        c_rows = _make_rows([("t1", True, "docs"), ("t2", True, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 0.5, b_rows)
        cp = _write_artifact(tmp_path, "candidate.json", 1.0, c_rows)
        report = ComparisonReport(BenchmarkResult(bp), BenchmarkResult(cp)).run()
        assert len(report["task_deltas"]) == 2


class TestBenchmarkRunner:
    def test_compare(self, tmp_path):
        b_rows = _make_rows([("t1", True, "docs"), ("t2", False, "edit")])
        c_rows = _make_rows([("t1", True, "docs"), ("t2", True, "edit")])
        bp = _write_artifact(tmp_path, "baseline.json", 0.5, b_rows)
        cp = _write_artifact(tmp_path, "candidate.json", 1.0, c_rows)
        runner = BenchmarkRunner()
        report = runner.compare(str(bp), str(cp))
        assert report["overall_delta"] == 0.5
