import hashlib
import json
import locale as locale_module
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import memory as memorylib
from .models import FakeModelClient
from .runtime import Owl, SessionStore
from .run_store import RunStore
from .task_state import STOP_REASON_FINAL_ANSWER_RETURNED
from .workspace import WorkspaceContext
from .trace_schema import TraceEvent
from .failure_analyzer import classify_failure
from .evaluators.outcome import OutcomeEvaluator
from .evaluators.process import ProcessEvaluator
from .evaluators.efficiency import EfficiencyEvaluator
from .evaluators.safety import SafetyEvaluator

BENCHMARK_SCHEMA_VERSION = 2
ARTIFACT_SCHEMA_VERSION = 1
DEFAULT_BENCHMARK_PATH = Path("benchmarks/coding_tasks.json")
DEFAULT_ARTIFACT_PATH = Path("benchmarks/benchmark-v1.json")
DEFAULT_MODEL_NAME = "FakeModelClient"
DEFAULT_MODEL_VERSION = "scripted-deterministic"
DEFAULT_TEMPERATURE = 0.0
DEFAULT_TOP_P = 1.0
DEFAULT_MAX_NEW_TOKENS = 64
DEFAULT_TIMEZONE = "Asia/Shanghai"

REQUIRED_BENCHMARK_KEYS = ("schema_version", "tasks")
REQUIRED_TASK_KEYS = (
    "id",
    "prompt",
    "fixture_repo",
    "allowed_tools",
    "step_budget",
    "expected_artifact",
    "verifier",
    "category",
)

TASK_FIXTURE_ARTIFACTS = {
    "bench_repo_readme": "README.md",
    "bench_repo_patch": "sample.txt",
    "bench_v2_context_noise": "config.yaml",
    "bench_v2_multistep_edit": "version.txt",
    "bench_v2_failure_modes": "config.yaml",
}

SCRIPTED_MODEL_OUTPUTS = {
    "readme_intro_locked": [
        '<tool name="patch_file" path="README.md"><old_text>This is a placeholder benchmark fixture.</old_text><new_text>This fixture is a locked benchmark workspace.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_schema_note": [
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the repo.</old_text><new_text>- The benchmark schema and baseline are fixed.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "readme_ordering_note": [
        '<tool name="patch_file" path="README.md"><old_text>- Placeholder note about the file layout.</old_text><new_text>- Deterministic file ordering keeps benchmark diffs stable.</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_beta_locked": [
        '<tool name="patch_file" path="sample.txt"><old_text>beta</old_text><new_text>beta-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_gamma_locked": [
        '<tool name="patch_file" path="sample.txt"><old_text>gamma</old_text><new_text>gamma-locked</new_text></tool>',
        "<final>Done.</final>",
    ],
    "sample_placeholder_delta": [
        '<tool name="patch_file" path="sample.txt"><old_text>placeholder</old_text><new_text>delta</new_text></tool>',
        "<final>Done.</final>",
    ],
    # ---- Behavioral Benchmark: Context Tasks ----
    "context_latest_override": [
        '<tool name="patch_file" path="config.yaml"><old_text>timeout: 30</old_text><new_text>timeout: 60</new_text></tool>',
        "<final>Done.</final>",
    ],
    "context_irrelevant_memory_filter": [
        '<tool>{"name":"read_file","args":{"path":"credentials.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="api_config.txt"><old_text>api_key: PLACEHOLDER</old_text><new_text>api_key: AKIA-TEST-7749</new_text></tool>',
        "<final>Done.</final>",
    ],
    "context_long_history_keep_target": [
        '<tool name="patch_file" path="report.txt"><old_text>ref: NONE</old_text><new_text>ref: launch-code-7749</new_text></tool>',
        '<tool name="patch_file" path="report.txt"><old_text>status: pending</old_text><new_text>status: confirmed</new_text></tool>',
        "<final>Done.</final>",
    ],
    "context_multi_file_disambiguation": [
        '<tool>{"name":"list_files","args":{"path":"config"}}</tool>',
        '<tool name="patch_file" path="config/service-a.yaml"><old_text>version: "1.0"</old_text><new_text>version: "2.0"</new_text></tool>',
        "<final>Done.</final>",
    ],
    # ---- Behavioral Benchmark: Workflow Tasks ----
    "workflow_search_read_patch": [
        '<tool>{"name":"search","args":{"pattern":"PLACEHOLDER_VERSION","path":"."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"version.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="version.txt"><old_text>version: PLACEHOLDER_VERSION</old_text><new_text>version: 3.1.0</new_text></tool>',
        "<final>Done.</final>",
    ],
    "workflow_verify_before_done": [
        '<tool name="patch_file" path="labels.txt"><old_text>label: old-label</old_text><new_text>label: new-label</new_text></tool>',
        '<tool>{"name":"read_file","args":{"path":"labels.txt","start":1,"end":20}}</tool>',
        "<final>Done.</final>",
    ],
    "workflow_avoid_duplicate_read": [
        '<tool>{"name":"read_file","args":{"path":"settings.txt","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="settings.txt"><old_text>mode: debug</old_text><new_text>mode: production</new_text></tool>',
        "<final>Done.</final>",
    ],
    "workflow_partial_signal_recovery": [
        '<tool>{"name":"search","args":{"pattern":"ERROR_CODE","path":"."}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"error_codes_full.py","start":1,"end":20}}</tool>',
        '<tool name="patch_file" path="error_codes_full.py"><old_text>ERROR_VERSION = 1</old_text><new_text>ERROR_VERSION = 2</new_text></tool>',
        "<final>Done.</final>",
    ],
    # ---- Failure Benchmark: Stop Reason Tasks ----
    # stop_reason_step_limit: step_budget=1, first tool call consumes the budget
    "stop_reason_step_limit": [
        '<tool>{"name":"read_file","args":{"path":"config.yaml","start":1,"end":20}}</tool>',
    ],
    # stop_reason_policy_block: run_shell gets blocked, then model gives up
    "stop_reason_policy_block": [
        '<tool>{"name":"run_shell","args":{"command":"echo cleanup","timeout":10}}</tool>',
        "<final>I cannot execute shell commands because approval was denied by policy.</final>",
    ],
}


def _git_value(args, fallback="", cwd=None):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip() or fallback
    except Exception:
        return fallback


def _current_locale():
    try:
        return locale_module.setlocale(locale_module.LC_CTYPE)
    except Exception:
        return locale_module.getdefaultlocale()[0] or "C"


def _now_in_timezone(timezone_name):
    return datetime.now(ZoneInfo(timezone_name)).strftime("%Y-%m-%dT%H:%M:%S%z")


def _default_benchmark_workspace_root(repo_root):
    workspace_root = Path(repo_root) / ".tmp" / "benchmark-workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    return workspace_root


def _artifact_path_for_task(task):
    # Allow per-task override via artifact_file field
    if "artifact_file" in task:
        return str(task["artifact_file"])
    fixture_repo_name = Path(str(task["fixture_repo"])).name
    if fixture_repo_name not in TASK_FIXTURE_ARTIFACTS:
        raise ValueError(f"unsupported fixture repo for artifact lookup: {fixture_repo_name}")
    return TASK_FIXTURE_ARTIFACTS[fixture_repo_name]


def _workspace_relative(path, workspace_root):
    return str(Path(path).resolve().relative_to(Path(workspace_root).resolve()))


def _scripted_outputs_for_task(task):
    outputs = SCRIPTED_MODEL_OUTPUTS.get(task["id"])
    if outputs is None:
        raise ValueError(f"no scripted model outputs for benchmark task: {task['id']}")
    return list(outputs)


def _fixture_snapshot_id(fixture_paths):
    sha = hashlib.sha256()
    for fixture_path in sorted({Path(path).resolve() for path in fixture_paths}, key=lambda path: str(path)):
        for path in sorted((item for item in fixture_path.rglob("*") if item.is_file()), key=lambda item: str(item.relative_to(fixture_path))):
            sha.update(str(fixture_path.name).encode("utf-8"))
            sha.update(b"\0")
            sha.update(str(path.relative_to(fixture_path)).encode("utf-8"))
            sha.update(b"\0")
            sha.update(path.read_bytes())
            sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_benchmark(data, repo_root=None):
    if not isinstance(data, dict):
        raise ValueError("benchmark must be a mapping")

    missing = [key for key in REQUIRED_BENCHMARK_KEYS if key not in data]
    if missing:
        raise ValueError(f"benchmark is missing required keys: {', '.join(missing)}")

    if int(data.get("schema_version", 0)) not in (1, 2):
        raise ValueError("unsupported benchmark schema_version")

    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("benchmark tasks must be a non-empty list")

    repo_root = Path(repo_root or Path.cwd()).resolve()
    seen_ids = set()
    normalized_tasks = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"benchmark task at index {index} must be a mapping")

        missing_task_keys = [key for key in REQUIRED_TASK_KEYS if key not in task]
        if missing_task_keys:
            raise ValueError(
                f"benchmark task {task.get('id', index)!r} is missing required keys: {', '.join(missing_task_keys)}"
            )

        task_id = str(task["id"]).strip()
        if not task_id:
            raise ValueError(f"benchmark task at index {index} has an empty id")
        if task_id in seen_ids:
            raise ValueError(f"duplicate benchmark task id: {task_id}")
        seen_ids.add(task_id)

        fixture_repo = repo_root / str(task["fixture_repo"])
        if not fixture_repo.is_dir():
            raise ValueError(f"benchmark task {task_id} fixture repo does not exist: {task['fixture_repo']}")

        allowed_tools = task["allowed_tools"]
        if not isinstance(allowed_tools, list) or not allowed_tools:
            raise ValueError(f"benchmark task {task_id} allowed_tools must be a non-empty list")
        normalized_allowed_tools = []
        for tool in allowed_tools:
            tool_name = str(tool).strip()
            if not tool_name:
                raise ValueError(f"benchmark task {task_id} has an empty allowed_tools entry")
            normalized_allowed_tools.append(tool_name)

        step_budget = int(task["step_budget"])
        if step_budget < 1:
            raise ValueError(f"benchmark task {task_id} step_budget must be positive")

        normalized_task = dict(task)
        normalized_task["id"] = task_id
        normalized_task["prompt"] = str(task["prompt"]).strip()
        normalized_task["fixture_repo"] = str(task["fixture_repo"]).strip()
        normalized_task["allowed_tools"] = normalized_allowed_tools
        normalized_task["step_budget"] = step_budget
        normalized_task["expected_artifact"] = str(task["expected_artifact"]).strip()
        normalized_task["verifier"] = str(task["verifier"]).strip()
        normalized_task["category"] = str(task["category"]).strip()
        # Schema v2 optional fields
        for key in (
            "expected_status", "expected_stop_reason", "expected_failure_category",
            "tags", "session_setup", "trace_expectations", "metrics_assertions",
            "artifact_file",
        ):
            if key in task:
                normalized_task[key] = task[key]
        normalized_tasks.append(normalized_task)

    normalized = dict(data)
    normalized["schema_version"] = BENCHMARK_SCHEMA_VERSION
    normalized["tasks"] = normalized_tasks
    return normalized


def load_benchmark(path=DEFAULT_BENCHMARK_PATH, repo_root=None):
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if repo_root is None:
        repo_root = path.resolve().parent.parent
    return validate_benchmark(data, repo_root=repo_root)


def summarize_rows(rows):
    rows = list(rows)
    passed = sum(1 for row in rows if row.get("passed") or row.get("status") == "pass")
    failed = len(rows) - passed
    failure_category_counts = {}
    for row in rows:
        if row.get("passed") or row.get("status") == "pass":
            continue
        category = str(row.get("failure_category") or "unknown")
        failure_category_counts[category] = failure_category_counts.get(category, 0) + 1

    total_tasks = len(rows)
    within_budget = sum(1 for row in rows if row.get("within_budget"))
    verifier_passes = sum(1 for row in rows if row.get("verifier_passed"))
    return {
        "total_tasks": total_tasks,
        "passed": passed,
        "failed": failed,
        "pass_rate": (passed / total_tasks) if total_tasks else 0.0,
        "within_budget": within_budget,
        "verifier_passes": verifier_passes,
        "failure_category_counts": failure_category_counts,
    }


def _apply_session_setup(agent, setup):
    """Pre-seed agent session with history and memory from task config."""
    if "history" in setup:
        for item in setup["history"]:
            agent.record(item)
    if "memory_notes" in setup:
        _now = datetime.now(timezone.utc).isoformat()
        for note in setup["memory_notes"]:
            agent.memory.append_note(
                note["text"],
                tags=tuple(note.get("tags", ())),
                created_at=note.get("created_at", _now),
            )
        agent.session["memory"] = agent.memory.to_dict()
        agent.session_store.save(agent.session)


# Task IDs that require custom model clients (not scripted FakeModelClient)
_CUSTOM_MODEL_TASKS = {
    "stop_reason_retry_limit": "retry_limit",
    "stop_reason_model_error": "model_error",
}


class BenchmarkEvaluator:
    def __init__(
        self,
        benchmark_path=DEFAULT_BENCHMARK_PATH,
        artifact_path=DEFAULT_ARTIFACT_PATH,
        workspace_root=None,
        model_name=DEFAULT_MODEL_NAME,
        model_version=DEFAULT_MODEL_VERSION,
        temperature=DEFAULT_TEMPERATURE,
        top_p=DEFAULT_TOP_P,
        max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
        timezone_name=DEFAULT_TIMEZONE,
        model_client_factory=None,
    ):
        self.benchmark_path = Path(benchmark_path)
        self.artifact_path = Path(artifact_path)
        self.repo_root = self.benchmark_path.resolve().parent.parent
        self.workspace_root = (
            Path(workspace_root)
            if workspace_root is not None
            else _default_benchmark_workspace_root(self.repo_root)
        )
        self.model_name = model_name
        self.model_version = model_version
        self.temperature = temperature
        self.top_p = top_p
        self.max_new_tokens = max_new_tokens
        self.timezone_name = timezone_name
        self.model_client_factory = model_client_factory

    def load(self):
        return load_benchmark(self.benchmark_path, repo_root=self.repo_root)

    def run(self):
        benchmark = self.load()
        rows = [self.run_task(task) for task in benchmark["tasks"]]
        summary = summarize_rows(rows)
        artifact = {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "captured_at": _now_in_timezone(self.timezone_name),
            "runtime": {
                "commit_sha": _git_value(["rev-parse", "HEAD"], cwd=self.repo_root),
                "branch": _git_value(["branch", "--show-current"], cwd=self.repo_root),
            },
            "benchmark": {
                "source": str(self.benchmark_path.resolve().relative_to(self.repo_root)),
                "task_count": len(benchmark["tasks"]),
            },
            "reproducibility": {
                "fixture_snapshot_id": _fixture_snapshot_id(
                    self.repo_root / str(task["fixture_repo"]) for task in benchmark["tasks"]
                ),
                "model_name": self.model_name,
                "model_version": self.model_version,
                "decoding": {
                    "temperature": self.temperature,
                    "top_p": self.top_p,
                    "max_new_tokens": self.max_new_tokens,
                },
                "timezone": self.timezone_name,
                "locale": _current_locale(),
            },
            "summary": summary,
            "failure_category_counts": summary["failure_category_counts"],
            "rows": rows,
        }
        self._write_artifact(artifact)
        return artifact

    def run_task(self, task):
        task = dict(task)
        fixture_source = self.repo_root / task["fixture_repo"]
        fixture_copy_root = self.workspace_root / task["id"] / fixture_source.name
        if fixture_copy_root.exists():
            shutil.rmtree(fixture_copy_root)
        fixture_copy_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(fixture_source, fixture_copy_root)

        workspace = WorkspaceContext.build(
            fixture_copy_root,
            repo_root_override=fixture_copy_root,
        )
        session_store = SessionStore(fixture_copy_root / ".owl" / "sessions")
        run_store = RunStore(fixture_copy_root / ".owl" / "runs")
        # ---- Model client dispatch ----
        custom_type = _CUSTOM_MODEL_TASKS.get(task["id"])
        if self.model_client_factory is not None:
            model_client = self.model_client_factory(task=task, workspace=workspace)
        elif custom_type == "retry_limit":
            from .benchmark_model_clients import RetryTriggeringModelClient
            model_client = RetryTriggeringModelClient()
        elif custom_type == "model_error":
            from .benchmark_model_clients import ErrorInjectingModelClient
            model_client = ErrorInjectingModelClient()
        else:
            model_client = FakeModelClient(_scripted_outputs_for_task(task))
        # ---- Approval policy override for failure tasks ----
        approval = "auto"
        if task.get("category") == "failure" and "policy_block" in task.get("tags", []):
            approval = "never"
        agent = Owl(
            model_client=model_client,
            workspace=workspace,
            session_store=session_store,
            run_store=run_store,
            approval_policy=approval,
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
        )

        # ---- Session setup (pre-seed history / memory) ----
        if "session_setup" in task:
            _apply_session_setup(agent, task["session_setup"])

        initial_history_empty = len(agent.session["history"]) == 0
        initial_memory_state = agent.memory.to_dict()
        initial_memory_empty = initial_memory_state == memorylib.default_memory_state()
        initial_task_summary_empty = not str(initial_memory_state["working"]["task_summary"]).strip()
        initial_episodic_notes_empty = not initial_memory_state["episodic_notes"]

        # ---- Run agent (with error handling for model_error tasks) ----
        try:
            final_answer = agent.ask(task["prompt"])
        except RuntimeError as exc:
            if custom_type == "model_error":
                final_answer = str(exc)
                # Agent ask() failed before finalizing task_state; set it manually
                agent.current_task_state.stop_model_error(str(exc))
            else:
                raise
        task_state = agent.current_task_state
        run_dir = Path(agent.current_run_dir) if agent.current_run_dir else None
        task_state_path = agent.run_store.task_state_path(task_state) if run_dir else None
        report_path = agent.run_store.report_path(task_state) if run_dir else None
        try:
            report = agent.run_store.load_report(task_state.run_id) if task_state.run_id else None
        except (FileNotFoundError, json.JSONDecodeError):
            report = None

        artifact_path = _artifact_path_for_task(task)
        artifact_file = fixture_copy_root / artifact_path
        expected_artifact_exists = artifact_file.exists()
        artifact_digest = _digest_file(artifact_file) if expected_artifact_exists else ""

        verifier = subprocess.run(
            task["verifier"],
            cwd=fixture_copy_root,
            shell=True,
            capture_output=True,
            text=True,
        )

        within_budget = task_state.tool_steps <= int(task["step_budget"])
        verifier_passed = verifier.returncode == 0
        non_failure_stop_reason = task_state.stop_reason == STOP_REASON_FINAL_ANSWER_RETURNED

        # ---- Pass/fail logic ----
        expected_status = task.get("expected_status")
        expected_stop_reason = task.get("expected_stop_reason")

        if expected_status is not None or expected_stop_reason is not None:
            # Failure-mode task: "expected failure is pass"
            status_match = expected_status is None or task_state.status == expected_status
            stop_reason_match = expected_stop_reason is None or task_state.stop_reason == expected_stop_reason
            passed = status_match and stop_reason_match
        else:
            # Normal task: original logic
            passed = within_budget and verifier_passed and expected_artifact_exists and non_failure_stop_reason
        failure_category = None if passed else self._failure_category(
            within_budget=within_budget,
            verifier_passed=verifier_passed,
            expected_artifact_exists=expected_artifact_exists,
            non_failure_stop_reason=non_failure_stop_reason,
        )

        # ---- 四层评估 ----
        from .metrics import compute_metrics  # lazy to avoid circular import
        try:
            trace_dicts = run_store.load_trace(task_state) if task_state.run_id else []
        except Exception:
            trace_dicts = []
        trace_events = [TraceEvent.from_dict(e) for e in trace_dicts]
        ts_dict = task_state.to_dict()
        metrics = compute_metrics(trace_events, ts_dict)
        trace_failure_cat = classify_failure(ts_dict, trace_dicts, metrics)
        # evaluator 自己的 coarse failure category 也保留（用于 benchmark 层面区分）
        # 新增细粒度 failure_category 覆盖
        combined_failure_cat = trace_failure_cat or failure_category

        outcome_eval = OutcomeEvaluator().evaluate(trace_dicts, ts_dict, metrics, task_config=task)
        process_eval = ProcessEvaluator().evaluate(trace_dicts, ts_dict, metrics)
        efficiency_eval = EfficiencyEvaluator().evaluate(trace_dicts, ts_dict, metrics)
        safety_eval = SafetyEvaluator().evaluate(trace_dicts, ts_dict, metrics)

        overall_score = (
            outcome_eval["score"] * 0.4
            + process_eval["score"] * 0.3
            + efficiency_eval["score"] * 0.2
            + safety_eval["score"] * 0.1
        )

        # ---- Trace validation (L3) ----
        trace_validation = None
        try:
            from .trace_validator import validate_trace_completeness, validate_trace_order
            trace_validation = {
                "completeness": validate_trace_completeness(trace_dicts),
                "order": validate_trace_order(trace_dicts),
            }
        except Exception:
            trace_validation = {"error": "trace validation unavailable"}

        return {
            "id": task["id"],
            "prompt": task["prompt"],
            "fixture_repo": task["fixture_repo"],
            "fixture_copy_relpath": _workspace_relative(fixture_copy_root, self.workspace_root),
            "run_id": task_state.run_id,
            "run_dir_relpath": _workspace_relative(run_dir, self.workspace_root) if run_dir else "",
            "task_state_relpath": _workspace_relative(task_state_path, self.workspace_root) if task_state_path else "",
            "report_relpath": _workspace_relative(report_path, self.workspace_root) if report_path else "",
            "allowed_tools": list(task["allowed_tools"]),
            "step_budget": int(task["step_budget"]),
            "expected_artifact": task["expected_artifact"],
            "artifact_path": artifact_path,
            "artifact_exists": expected_artifact_exists,
            "artifact_digest": artifact_digest,
            "verifier": task["verifier"],
            "verifier_exit_code": verifier.returncode,
            "verifier_stdout": verifier.stdout,
            "verifier_stderr": verifier.stderr,
            "category": task["category"],
            "tags": list(task.get("tags", [])),
            "complexity": {"reasoning": "low", "tool": "low", "interaction": "low"},
            "status": "pass" if passed else "fail",
            "passed": passed,
            "failure_category": combined_failure_cat,
            "within_budget": within_budget,
            "verifier_passed": verifier_passed,
            "expected_artifact_exists": expected_artifact_exists,
            "non_failure_stop_reason": non_failure_stop_reason,
            "tool_steps": task_state.tool_steps,
            "attempts": task_state.attempts,
            "final_answer": final_answer,
            "stop_reason": task_state.stop_reason,
            "initial_history_empty": initial_history_empty,
            "initial_memory_empty": initial_memory_empty,
            "initial_task_summary_empty": initial_task_summary_empty,
            "initial_episodic_notes_empty": initial_episodic_notes_empty,
            "task_state": task_state.to_dict(),
            "report": report,
            "metrics": metrics,
            "evaluations": {
                "outcome": outcome_eval,
                "process": process_eval,
                "efficiency": efficiency_eval,
                "safety": safety_eval,
            },
            "overall_score": overall_score,
            "trace_validation": trace_validation,
        }

    def _failure_category(
        self,
        within_budget,
        verifier_passed,
        expected_artifact_exists,
        non_failure_stop_reason,
    ):
        if not expected_artifact_exists:
            return "missing_artifact"
        if not within_budget:
            return "budget_exceeded"
        if not verifier_passed:
            return "verifier_failed"
        if not non_failure_stop_reason:
            return "failure_stop_reason"
        return "unknown"

    def _write_artifact(self, artifact):
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _digest_file(path):
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_fixed_benchmark(
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=DEFAULT_ARTIFACT_PATH,
    workspace_root=None,
    model_name=DEFAULT_MODEL_NAME,
    model_version=DEFAULT_MODEL_VERSION,
    temperature=DEFAULT_TEMPERATURE,
    top_p=DEFAULT_TOP_P,
    max_new_tokens=DEFAULT_MAX_NEW_TOKENS,
    timezone_name=DEFAULT_TIMEZONE,
    model_client_factory=None,
):
    evaluator = BenchmarkEvaluator(
        benchmark_path=benchmark_path,
        artifact_path=artifact_path,
        workspace_root=workspace_root,
        model_name=model_name,
        model_version=model_version,
        temperature=temperature,
        top_p=top_p,
        max_new_tokens=max_new_tokens,
        timezone_name=timezone_name,
        model_client_factory=model_client_factory,
    )
    return evaluator.run()
