import json

from owl.skill_candidate_registry import SkillCandidateRegistry
from owl.skill_memory_service import SkillMemoryService, main


def _write_registry(repo_root):
    registry = SkillCandidateRegistry()
    candidate = registry.register(
        "pytest_import_error",
        "Fix pytest import errors by checking package config and test path setup",
        "run-1",
        procedure_steps=[
            "Check pyproject package config",
            "Check tests/conftest.py path setup",
            "Run targeted pytest before full suite",
        ],
        trigger_conditions=["pytest fails with ModuleNotFoundError"],
        anti_patterns=["Do not rewrite package layout before checking editable install"],
        applicable_repo_paths=["tests/conftest.py", "pyproject.toml"],
    )
    candidate.confidence = 0.9
    candidate.stage = "skill_candidate"
    registry_path = SkillMemoryService.default_registry_path(repo_root)
    registry.save(registry_path)
    return registry_path


def test_recall_skill_returns_context_packet(tmp_path):
    _write_registry(tmp_path)
    service = SkillMemoryService(tmp_path)

    packet = service.recall_skill("pytest ModuleNotFoundError import failure", max_tokens=80)

    assert packet["source"] == "owl-skill"
    assert packet["confidence"] > 0
    assert packet["matches"][0]["pattern_type"] == "pytest_import_error"
    assert packet["matches"][0]["stage"] == "skill_candidate"
    assert "Check pyproject package config" in packet["prompt_injection"]
    assert packet["matches"][0]["when_to_use"] == ["pytest fails with ModuleNotFoundError"]


def test_recall_skill_returns_empty_packet_when_no_match(tmp_path):
    _write_registry(tmp_path)
    service = SkillMemoryService(tmp_path)

    packet = service.recall_skill("unrelated database migration", max_tokens=80)

    assert packet["matches"] == []
    assert packet["confidence"] == 0.0
    assert packet["prompt_injection"] == "Relevant owl workflows:\n- none"


def test_export_skills_sanitizes_private_values(tmp_path):
    registry = SkillCandidateRegistry()
    registry.register(
        "secret_path",
        r"Use E:\closed\project\auth.py with sk-secret-token-12345",
        "run-1",
        procedure_steps=[r"Open E:\closed\project\auth.py"],
        trigger_conditions=["OPENAI_API_KEY=secret"],
        applicable_repo_paths=[r"E:\closed\project\auth.py"],
    )
    registry.save(SkillMemoryService.default_registry_path(tmp_path))
    service = SkillMemoryService(tmp_path)

    payload = service.export_skills(tmp_path / "export" / "skills.json", sanitize=True)
    text = json.dumps(payload, ensure_ascii=False)

    assert payload["sanitized"] is True
    assert payload["candidate_count"] == 1
    assert "E:\\closed" not in text
    assert "sk-secret-token" not in text
    assert "OPENAI_API_KEY" not in text
    assert "<redacted-path>" in text
    assert "<redacted-secret>" in text


def test_external_write_observation_is_rejected_by_default(tmp_path):
    service = SkillMemoryService(tmp_path)

    result = service.write_observation(event={"tool": "read_file"})

    assert result["status"] == "rejected"
    assert result["event_received"] is True


def test_owl_skill_cli_recall_outputs_json(tmp_path, capsys):
    _write_registry(tmp_path)

    exit_code = main([
        "recall",
        "--repo",
        str(tmp_path),
        "--query",
        "pytest ModuleNotFoundError",
    ])

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert exit_code == 0
    assert data["source"] == "owl-skill"
    assert data["matches"][0]["pattern_type"] == "pytest_import_error"
