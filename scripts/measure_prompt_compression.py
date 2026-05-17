"""Measure real prompt compression ratios by running the evaluator benchmark."""

import json
import sys
from pathlib import Path

# 确保项目根目录在 path 中
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owl.evaluator import run_fixed_benchmark


def main():
    # 运行 benchmark
    artifact_path = "benchmarks/_tmp_metrics.json"
    print("Running benchmark...")
    result = run_fixed_benchmark(
        benchmark_path="benchmarks/coding_tasks.json",
        artifact_path=artifact_path,
        model_name="FakeModelClient",
        max_new_tokens=64,
    )

    rows = result.get("rows", [])
    print(f"\nBenchmark rows: {len(rows)}")

    # 从 result 中直接提取每步的 prompt 长度
    all_prompt_chars = []
    all_history_chars = []
    all_memory_chars = []
    all_prefix_chars = []

    for row in rows:
        report = row.get("report", {})
        steps = report.get("steps", [])

        for step in steps:
            # trace events in each step
            trace_events = step.get("trace", [])
            if isinstance(trace_events, str):
                try:
                    trace_events = json.loads(trace_events)
                except Exception:
                    continue

            for evt in trace_events:
                if isinstance(evt, str):
                    try:
                        evt = json.loads(evt)
                    except Exception:
                        continue
                ename = evt.get("event_name") or evt.get("event", "")
                if ename == "prompt_built":
                    meta = evt.get("metadata", {})
                    pm = meta.get("prompt_metadata", {})
                    if pm and pm.get("prompt_chars"):
                        all_prompt_chars.append(pm["prompt_chars"])
                        all_history_chars.append(pm.get("history_chars", 0))
                        all_memory_chars.append(pm.get("memory_chars", 0))
                        all_prefix_chars.append(pm.get("prefix_chars", 0))

    # 也直接从 .owl/runs 收集（benchmark 用 fixture repo）
    runs_root = Path(".owl/runs")
    for run_dir in sorted(runs_root.iterdir()):
        trace_path = run_dir / "trace.jsonl"
        if not trace_path.exists():
            continue
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            try:
                evt = json.loads(line)
            except Exception:
                continue
            ename = evt.get("event_name") or evt.get("event", "")
            if ename == "prompt_built":
                meta = evt.get("metadata", {})
                pm = meta.get("prompt_metadata", {})
                if pm and pm.get("prompt_chars") and pm["prompt_chars"] > 0:
                    # 避免重复
                    entry = (pm["prompt_chars"], pm.get("history_chars", 0))
                    if entry not in [(p, h) for p, h in zip(all_prompt_chars, all_history_chars)]:
                        all_prompt_chars.append(pm["prompt_chars"])
                        all_history_chars.append(pm.get("history_chars", 0))
                        all_memory_chars.append(pm.get("memory_chars", 0))
                        all_prefix_chars.append(pm.get("prefix_chars", 0))

    print("\n=== PROMPT LENGTH DATA ===")
    print(f"Total prompt_built entries: {len(all_prompt_chars)}")

    if all_prompt_chars:
        avg = sum(all_prompt_chars) / len(all_prompt_chars)
        print(f"Average prompt length: {avg:.0f} chars")
        print(f"Max: {max(all_prompt_chars)}, Min: {min(all_prompt_chars)}")
        print(f"Avg history: {sum(all_history_chars)/len(all_history_chars):.0f}")
        print(f"Avg memory: {sum(all_memory_chars)/len(all_memory_chars):.0f}")
        print(f"Avg prefix: {sum(all_prefix_chars)/len(all_prefix_chars):.0f}")

    # 清理
    Path(artifact_path).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
