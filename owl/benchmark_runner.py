"""Benchmark 批量运行与跨版本对比。

BenchmarkRunner 负责批量跑 benchmark，并输出跨版本对照结果。
compare() 可以对比 baseline 和 candidate 两轮 benchmark 结果，解释"为什么变好或变差"。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class BenchmarkResult:
    """单次 benchmark 运行结果。"""

    def __init__(self, artifact_path: str | Path):
        self.artifact_path = Path(artifact_path)
        self._data: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = json.loads(self.artifact_path.read_text(encoding="utf-8"))
        return self._data

    @property
    def summary(self) -> dict[str, Any]:
        return self.load().get("summary", {})

    @property
    def rows(self) -> list[dict[str, Any]]:
        return self.load().get("rows", [])

    @property
    def run_metadata(self) -> dict[str, Any]:
        return self.load().get("runtime", {})

    @property
    def task_count(self) -> int:
        return self.summary.get("total_tasks", len(self.rows))

    @property
    def pass_rate(self) -> float:
        return self.summary.get("pass_rate", 0.0)

    def per_category_pass_rate(self) -> dict[str, float]:
        counts: dict[str, dict[str, int]] = {}
        for row in self.rows:
            cat = str(row.get("category", "unknown"))
            if cat not in counts:
                counts[cat] = {"passed": 0, "total": 0}
            counts[cat]["total"] += 1
            if row.get("passed"):
                counts[cat]["passed"] += 1
        return {
            cat: (v["passed"] / v["total"]) if v["total"] else 0.0
            for cat, v in counts.items()
        }

    def to_dict(self) -> dict[str, Any]:
        return self.load()


class ComparisonReport:
    """baseline vs candidate 对比报告。"""

    def __init__(self, baseline: BenchmarkResult, candidate: BenchmarkResult):
        self.baseline = baseline
        self.candidate = candidate

    def run(self) -> dict[str, Any]:
        b_summary = self.baseline.summary
        c_summary = self.candidate.summary

        b_rows = self.baseline.rows
        c_rows = self.candidate.rows
        b_row_map = {r["id"]: r for r in b_rows}
        c_row_map = {r["id"]: r for r in c_rows}

        # 总体指标
        overall_delta = c_summary.get("pass_rate", 0.0) - b_summary.get("pass_rate", 0.0)

        # 按类别对比
        b_cat = self.baseline.per_category_pass_rate()
        c_cat = self.candidate.per_category_pass_rate()
        all_cats = set(b_cat) | set(c_cat)
        per_category_delta = {}
        for cat in all_cats:
            delta = c_cat.get(cat, 0.0) - b_cat.get(cat, 0.0)
            per_category_delta[cat] = {
                "baseline": b_cat.get(cat, 0.0),
                "candidate": c_cat.get(cat, 0.0),
                "delta": delta,
            }

        # 逐 task 对比
        task_deltas: list[dict[str, Any]] = []
        for task_id in sorted(set(list(b_row_map) + list(c_row_map))):
            b_row = b_row_map.get(task_id, {})
            c_row = c_row_map.get(task_id, {})
            task_deltas.append({
                "task_id": task_id,
                "baseline_passed": bool(b_row.get("passed")),
                "candidate_passed": bool(c_row.get("passed")),
                "delta": int(c_row.get("passed", False)) - int(b_row.get("passed", False)),
            })

        regressions = [t for t in task_deltas if t["delta"] < 0]
        improvements = [t for t in task_deltas if t["delta"] > 0]

        return {
            "overall_delta": overall_delta,
            "baseline_pass_rate": b_summary.get("pass_rate", 0.0),
            "candidate_pass_rate": c_summary.get("pass_rate", 0.0),
            "baseline_task_count": self.baseline.task_count,
            "candidate_task_count": self.candidate.task_count,
            "per_category_delta": per_category_delta,
            "regression_count": len(regressions),
            "improvement_count": len(improvements),
            "regressions": [
                {"task_id": t["task_id"], "baseline_passed": t["baseline_passed"], "candidate_passed": t["candidate_passed"]}
                for t in regressions
            ],
            "improvements": [
                {"task_id": t["task_id"], "baseline_passed": t["baseline_passed"], "candidate_passed": t["candidate_passed"]}
                for t in improvements
            ],
            "task_deltas": task_deltas,
        }


class BenchmarkRunner:
    """Benchmark 批量运行器。"""

    def __init__(self, evaluator_module=None):
        self._evaluator_module = evaluator_module

    def run(
        self,
        benchmark_path: str | Path,
        artifact_path: str | Path,
        workspace_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """运行一次 benchmark，返回 artifact 并写入文件。"""
        if self._evaluator_module is None:
            from owl import evaluator as _ev
            self._evaluator_module = _ev

        result = self._evaluator_module.run_fixed_benchmark(
            benchmark_path=str(benchmark_path),
            artifact_path=str(artifact_path),
            workspace_root=str(workspace_root) if workspace_root else None,
        )
        return result

    def compare(
        self,
        baseline_path: str | Path,
        candidate_path: str | Path,
    ) -> dict[str, Any]:
        """对比两个 benchmark artifact，输出变化摘要。"""
        baseline = BenchmarkResult(baseline_path)
        candidate = BenchmarkResult(candidate_path)
        report = ComparisonReport(baseline, candidate)
        return report.run()
