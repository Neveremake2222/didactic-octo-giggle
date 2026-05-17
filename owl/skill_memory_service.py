"""Facade for recalling and exporting transferable owl workflow skills.

The service is intentionally read-only for external recall. It loads the local
skill registry, ranks reusable workflow candidates, and returns compact context
packets that another coding agent can inject into its prompt.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .skill_candidate_registry import (
    DEFAULT_SKILL_REGISTRY_PATH,
    SkillCandidate,
    SkillCandidateRegistry,
)

DEFAULT_CONTEXT_PACKET_TOKEN_LIMIT = 1200
EXPORT_VERSION = 1
SECRET_VALUE_RE = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{8,}|[A-Za-z0-9_]*API_KEY[A-Za-z0-9_=:-]*)"
)
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+")
POSIX_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w.-])/(?:[^\s\"']+/)+[^\s\"']+")


@dataclass
class SkillContextPacket:
    """Structured skill recall result for prompt injection."""

    query: str
    matches: list[dict[str, Any]] = field(default_factory=list)
    max_tokens: int = DEFAULT_CONTEXT_PACKET_TOKEN_LIMIT

    @property
    def confidence(self) -> float:
        if not self.matches:
            return 0.0
        return float(self.matches[0].get("confidence", 0.0))

    @property
    def prompt_injection(self) -> str:
        if not self.matches:
            return "Relevant owl workflows:\n- none"
        lines = ["Relevant owl workflows:"]
        for item in self.matches:
            lines.append(
                f"- {item['title']} "
                f"(confidence {item['confidence']:.2f}, stage {item['stage']})"
            )
            for step in item.get("steps", [])[:3]:
                lines.append(f"  - {step}")
            for caution in item.get("cautions", [])[:2]:
                lines.append(f"  - caution: {caution}")
        return _clip_words("\n".join(lines), self.max_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": "owl-skill",
            "query": self.query,
            "confidence": self.confidence,
            "matches": self.matches,
            "max_tokens": self.max_tokens,
            "prompt_injection": self.prompt_injection,
        }


class SkillMemoryService:
    """Read-only facade around the local skill registry."""

    def __init__(self, repo_root: str | Path, registry_path: str | Path | None = None):
        self.repo_root = Path(repo_root)
        self.registry_path = Path(registry_path) if registry_path else self.default_registry_path(self.repo_root)

    @staticmethod
    def default_registry_path(repo_root: str | Path) -> Path:
        return Path(repo_root) / ".owl" / DEFAULT_SKILL_REGISTRY_PATH

    def load_registry(self) -> SkillCandidateRegistry:
        return SkillCandidateRegistry.load(self.registry_path)

    def recall_skill(
        self,
        query: str,
        repo_profile: str = "",
        error_signature: str | None = None,
        max_tokens: int = DEFAULT_CONTEXT_PACKET_TOKEN_LIMIT,
        top_k: int = 3,
    ) -> dict[str, Any]:
        """Return a compact context packet for the best matching skills."""
        registry = self.load_registry()
        search_text = " ".join(part for part in (query, repo_profile, error_signature or "") if part)
        ranked = self._rank_candidates(registry.all_candidates(), search_text)
        matches = [self._candidate_to_match(candidate, score) for score, candidate in ranked[:top_k] if score > 0]
        packet = SkillContextPacket(query=query, matches=matches, max_tokens=max_tokens)
        return packet.to_dict()

    def export_skills(
        self,
        destination: str | Path,
        sanitize: bool = True,
        min_stage: str = "semantic_fact",
    ) -> dict[str, Any]:
        """Export skill candidates to a portable JSON file."""
        registry = self.load_registry()
        candidates = [
            candidate.to_dict()
            for candidate in registry.all_candidates()
            if _stage_rank(candidate.stage) >= _stage_rank(min_stage)
        ]
        if sanitize:
            candidates = [_sanitize_obj(candidate) for candidate in candidates]

        payload = {
            "version": EXPORT_VERSION,
            "source": "owl-skill-export",
            "sanitized": bool(sanitize),
            "candidate_count": len(candidates),
            "candidates": candidates,
        }
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return payload

    def compact_run(self, repo_root: str | Path | None = None, run_id: str = "") -> dict[str, Any]:
        """Placeholder for future service-level compaction orchestration."""
        return {
            "status": "not_implemented",
            "repo_root": str(repo_root or self.repo_root),
            "run_id": run_id,
            "reason": "runtime compaction is currently owned by Owl.ask() finalization",
        }

    def write_observation(self, repo_root: str | Path | None = None, event: dict[str, Any] | None = None) -> dict[str, Any]:
        """External writes are disabled until a safe write contract is defined."""
        return {
            "status": "rejected",
            "repo_root": str(repo_root or self.repo_root),
            "reason": "external skill service is read-only by default",
            "event_received": bool(event),
        }

    def _rank_candidates(
        self,
        candidates: list[SkillCandidate],
        query: str,
    ) -> list[tuple[float, SkillCandidate]]:
        query_tokens = _tokens(query)
        ranked: list[tuple[float, SkillCandidate]] = []
        for candidate in candidates:
            haystack = " ".join(
                [
                    candidate.pattern_type,
                    candidate.description,
                    " ".join(candidate.trigger_conditions),
                    " ".join(candidate.procedure_steps),
                    " ".join(candidate.applicable_repo_paths),
                ]
            )
            candidate_tokens = _tokens(haystack)
            if not query_tokens or not candidate_tokens:
                score = 0.0
            else:
                matched = query_tokens & candidate_tokens
                if not matched:
                    score = 0.0
                else:
                    overlap = len(matched) / len(query_tokens)
                    score = min(1.0, overlap * 0.75 + candidate.confidence * 0.25)
            ranked.append((score, candidate))
        return sorted(ranked, key=lambda item: (item[0], item[1].confidence), reverse=True)

    def _candidate_to_match(self, candidate: SkillCandidate, score: float) -> dict[str, Any]:
        return {
            "skill_id": candidate.candidate_id,
            "title": candidate.description,
            "pattern_type": candidate.pattern_type,
            "stage": candidate.stage,
            "confidence": round(max(score, candidate.confidence), 4),
            "when_to_use": list(candidate.trigger_conditions),
            "steps": list(candidate.procedure_steps),
            "verification": [],
            "cautions": list(candidate.anti_patterns),
            "applicable_repo_paths": list(candidate.applicable_repo_paths),
            "usage_count": candidate.successful_uses + candidate.failed_uses,
            "success_count": candidate.successful_uses,
        }


def _stage_rank(stage: str) -> int:
    stages = ["semantic_fact", "procedure_candidate", "skill_candidate", "established_skill"]
    try:
        return stages.index(stage)
    except ValueError:
        return -1


def _tokens(text: str) -> set[str]:
    return {token for token in re.split(r"[^a-zA-Z0-9_./-]+", text.lower()) if len(token) >= 2}


def _clip_words(text: str, max_tokens: int) -> str:
    words = text.split()
    if len(words) <= max_tokens:
        return text
    return " ".join(words[:max_tokens]).rstrip() + "..."


def _sanitize_text(text: str) -> str:
    text = SECRET_VALUE_RE.sub("<redacted-secret>", text)
    text = WINDOWS_ABSOLUTE_PATH_RE.sub("<redacted-path>", text)
    return POSIX_ABSOLUTE_PATH_RE.sub("<redacted-path>", text)


def _sanitize_obj(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_obj(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_obj(item) for key, item in value.items()}
    return value


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="owl-skill", description="Recall or export owl workflow skills.")
    sub = parser.add_subparsers(dest="command", required=True)

    recall = sub.add_parser("recall", help="Recall matching workflow skills.")
    recall.add_argument("--repo", required=True)
    recall.add_argument("--query", required=True)
    recall.add_argument("--repo-profile", default="")
    recall.add_argument("--error-signature", default="")
    recall.add_argument("--max-tokens", type=int, default=DEFAULT_CONTEXT_PACKET_TOKEN_LIMIT)
    recall.add_argument("--top-k", type=int, default=3)

    export = sub.add_parser("export", help="Export workflow skills.")
    export.add_argument("--repo", required=True)
    export.add_argument("--out", required=True)
    export.add_argument("--sanitize", action="store_true")
    export.add_argument("--min-stage", default="semantic_fact")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    service = SkillMemoryService(args.repo)
    if args.command == "recall":
        packet = service.recall_skill(
            query=args.query,
            repo_profile=args.repo_profile,
            error_signature=args.error_signature or None,
            max_tokens=args.max_tokens,
            top_k=args.top_k,
        )
        print(json.dumps(packet, indent=2, ensure_ascii=False))
        return 0
    if args.command == "export":
        payload = service.export_skills(args.out, sanitize=args.sanitize, min_stage=args.min_stage)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
