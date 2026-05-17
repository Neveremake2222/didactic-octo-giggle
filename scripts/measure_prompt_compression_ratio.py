"""Measure prompt compression ratios from trace data.

Extracts raw_chars vs rendered_chars per section from prompt_built events,
computing real compression ratios across the full prompt assembly pipeline.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from owl.evaluator import run_fixed_benchmark


def collect_entries():
    """Collect all prompt_built events from benchmark and stored runs."""
    artifact_path = "benchmarks/_tmp_metrics.json"
    print("Running benchmark...")
    result = run_fixed_benchmark(
        benchmark_path="benchmarks/coding_tasks.json",
        artifact_path=artifact_path,
        model_name="FakeModelClient",
        max_new_tokens=64,
    )

    entries = []

    # From benchmark result
    for row in result.get("rows", []):
        task_id = row.get("id", "?")
        for step in row.get("report", {}).get("steps", []):
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
                    meta = evt.get("prompt_metadata") or {}
                    e = _extract_entry(meta, task_id)
                    if e:
                        entries.append(e)

    # From .owl/runs
    runs_root = Path(".owl/runs")
    if runs_root.exists():
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
                    meta = evt.get("prompt_metadata") or {}
                    e = _extract_entry(meta, run_dir.name)
                    if e:
                        entries.append(e)

    return entries


def _extract_entry(meta, source_id):
    """Extract per-section raw/rendered data from a prompt_built event."""
    sections = meta.get("sections", {})
    if not sections:
        return None

    entry = {
        "source": source_id,
        "prompt_chars": meta.get("prompt_chars", 0),
        "prompt_over_budget": meta.get("prompt_over_budget", False),
        "budget": meta.get("prompt_budget_chars", 12000),
        "budget_reductions": meta.get("budget_reductions", []),
        "section_data": {},
    }

    total_raw = 0
    total_rendered = 0
    for name, info in sections.items():
        raw = info.get("raw_chars", 0)
        rendered = info.get("rendered_chars", 0)
        budget = info.get("budget_chars")
        entry["section_data"][name] = {
            "raw": raw,
            "rendered": rendered,
            "budget": budget,
        }
        total_raw += raw
        total_rendered += rendered

    entry["total_raw"] = total_raw
    entry["total_rendered"] = total_rendered
    return entry


def print_report(entries):
    if not entries:
        print("No entries found.")
        return

    print(f"\n{'=' * 65}")
    print("PROMPT COMPRESSION MEASUREMENT REPORT")
    print(f"{'=' * 65}")

    # Filter out entries where all budgets are anomalously small (< 200)
    # These are test/fixture runs with artificially low budgets, not real compression
    normal = []
    for e in entries:
        budgets = [v.get("budget") for v in e["section_data"].values() if v.get("budget") is not None]
        if budgets and max(budgets) < 200:
            continue
        normal.append(e)

    filtered = len(entries) - len(normal)
    if filtered > 0:
        print(f"Filtered {filtered} anomalous low-budget entries")
    entries = normal
    print(f"Total prompt_built events: {len(entries)}")

    # Overall prompt compression
    raw_totals = [e["total_raw"] for e in entries]
    rendered_totals = [e["total_rendered"] for e in entries]
    prompt_chars = [e["prompt_chars"] for e in entries]
    over_budget = sum(1 for e in entries if e["prompt_over_budget"])

    avg_raw = sum(raw_totals) / len(raw_totals)
    avg_rendered = sum(rendered_totals) / len(rendered_totals)
    avg_prompt = sum(prompt_chars) / len(prompt_chars)

    print(f"\n--- Overall Prompt Assembly ---")
    print(f"  Avg raw sum (all sections):     {avg_raw:.0f} chars")
    print(f"  Avg rendered sum (all sections): {avg_rendered:.0f} chars")
    print(f"  Avg final prompt_chars:          {avg_prompt:.0f} chars")
    print(f"  Prompt budget:                   {entries[0]['budget']} chars")
    print(f"  Over-budget events:              {over_budget}/{len(entries)}")

    # Per-entry compression ratios (computed early for overall stats)
    per_entry_ratio = []
    for e in entries:
        raw = e["total_raw"]
        rendered = e["prompt_chars"]
        if raw > 0:
            per_entry_ratio.append((raw - rendered) / raw * 100)

    if per_entry_ratio:
        overall_reduction = sum(per_entry_ratio) / len(per_entry_ratio)
        print(f"  Overall avg compression rate:   {overall_reduction:.2f}%")

    # Per-section compression
    section_names = ["prefix", "memory", "relevant_memory", "history", "current_request"]
    print(f"\n--- Per-Section Compression ---")
    print(f"  {'Section':<20} {'Avg Raw':>8} {'Avg Rendered':>12} {'Avg Budget':>10} {'Compression':>12}")
    print(f"  {'-' * 20} {'-' * 8} {'-' * 12} {'-' * 10} {'-' * 12}")

    for name in section_names:
        raw_vals = [e["section_data"].get(name, {}).get("raw", 0) for e in entries if name in e["section_data"]]
        rendered_vals = [e["section_data"].get(name, {}).get("rendered", 0) for e in entries if name in e["section_data"]]
        budget_vals = [e["section_data"].get(name, {}).get("budget") for e in entries if name in e["section_data"] and e["section_data"][name].get("budget") is not None]

        if not raw_vals:
            continue

        avg_raw_s = sum(raw_vals) / len(raw_vals)
        avg_rendered_s = sum(rendered_vals) / len(rendered_vals)
        avg_budget_s = sum(b for b in budget_vals if b is not None) / len(budget_vals) if budget_vals else 0

        comp_pct = (avg_raw_s - avg_rendered_s) / avg_raw_s * 100 if avg_raw_s > 0 else 0
        budget_str = f"{avg_budget_s:.0f}" if budget_vals else "unlimited"
        print(f"  {name:<20} {avg_raw_s:>8.0f} {avg_rendered_s:>12.0f} {budget_str:>10} {comp_pct:>11.2f}%")

    # Budget reductions
    reductions = [r for e in entries for r in e.get("budget_reductions", [])]
    if reductions:
        print(f"\n--- Budget Reductions Applied ---")
        print(f"  Total reductions: {len(reductions)}")
        print(f"  Reductions: {reductions[:20]}")

    # Distribution of prompt lengths
    print(f"\n--- Prompt Length Distribution ---")
    sorted_prompts = sorted(prompt_chars)
    p25 = sorted_prompts[len(sorted_prompts) // 4]
    p50 = sorted_prompts[len(sorted_prompts) // 2]
    p75 = sorted_prompts[3 * len(sorted_prompts) // 4]
    print(f"  Min:  {min(prompt_chars):.0f}")
    print(f"  P25:  {p25:.0f}")
    print(f"  P50:  {p50:.0f}")
    print(f"  P75:  {p75:.0f}")
    print(f"  Max:  {max(prompt_chars):.0f}")

    # Per-entry compression details
    per_entry_raw = [e["total_raw"] for e in entries if e["total_raw"] > 0]
    per_entry_rendered = [e["prompt_chars"] for e in entries if e["total_raw"] > 0]

    avg_ratio = sum(per_entry_ratio) / len(per_entry_ratio) if per_entry_ratio else 0
    max_ratio = max(per_entry_ratio) if per_entry_ratio else 0
    min_ratio = min(per_entry_ratio) if per_entry_ratio else 0
    idx_max = per_entry_ratio.index(max_ratio) if per_entry_ratio else 0
    idx_min = per_entry_ratio.index(min_ratio) if per_entry_ratio else 0

    # Key metric for resume
    print(f"\n{'=' * 65}")
    print("KEY METRICS FOR RESUME:")
    print(f"{'=' * 65}")
    print(f"  Entries analyzed:  {len(entries)}")
    print(f"  Budget cap:        {entries[0]['budget']} chars, over-budget {over_budget}/{len(entries)}")
    print(f"")
    print(f"  Per-entry compression (raw -> rendered):")
    print(f"    Avg ratio:  {avg_ratio:.1f}%  (avg raw {sum(per_entry_raw)/len(per_entry_raw):.0f} -> avg rendered {sum(per_entry_rendered)/len(per_entry_rendered):.0f})")
    print(f"    Max ratio:  {max_ratio:.1f}%  ({per_entry_raw[idx_max]} -> {per_entry_rendered[idx_max]})")
    print(f"    Min ratio:  {min_ratio:.1f}%  ({per_entry_raw[idx_min]} -> {per_entry_rendered[idx_min]})")
    print(f"    Median:     {sorted(per_entry_ratio)[len(per_entry_ratio)//2]:.1f}%")
    print(f"{'=' * 65}")


if __name__ == "__main__":
    entries = collect_entries()
    print_report(entries)
    Path("benchmarks/_tmp_metrics.json").unlink(missing_ok=True)
