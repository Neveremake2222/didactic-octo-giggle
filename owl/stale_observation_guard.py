"""Working Memory 陈旧观察守护。

在工具执行后，检查 working memory 中的 observations 是否基于过期文件内容。
如果文件内容已变化，标记并移除对应的 observation。

工作流程：
  1. StaleObservationGuard.check_working_memory()
     遍历所有 observations，提取其中的文件路径
     与 FileFingerprintTracker 中的记录对比
     返回陈旧的 observations 列表

  2. StaleObservationGuard.remove_stale()
     从 working memory 中移除指定的陈旧 observations
     返回移除数量

本模块由 runtime.py 在工具执行循环中调用，受 stale_guard feature flag 控制。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .memory_validity import FileFingerprintTracker
from .memory_utils import extract_path_from_observation
from .working_memory import WorkingMemory


# ---------------------------------------------------------------------------
# StaleObservation
# ---------------------------------------------------------------------------


@dataclass
class StaleObservation:
    """一条被判定为陈旧的观察记录。"""

    observation_id: str
    file_path: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "file_path": self.file_path,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# StaleObservationGuard
# ---------------------------------------------------------------------------


class StaleObservationGuard:
    """Working Memory 陈旧观察守护。

    使用方式：
      guard = StaleObservationGuard()
      stale = guard.check_working_memory(wm, tracker)
      removed = guard.remove_stale(wm, stale)
    """

    def check_working_memory(
        self,
        wm: WorkingMemory,
        tracker: FileFingerprintTracker,
    ) -> list[StaleObservation]:
        """检查 working memory 中的 observations 是否基于过期文件内容。

        对每条 observation：
          1. 提取其中的文件路径
          2. 在 tracker 中查找历史 fingerprint
          3. 读取当前文件内容，计算 fingerprint
          4. 如果不一致，标记为 stale

        Returns:
            陈旧观察列表。
        """
        stale: list[StaleObservation] = []

        observations = getattr(wm, "recent_observations", [])
        for obs in observations:
            file_path = getattr(obs, "file_path", "")
            if not file_path:
                # 尝试从 summary 提取路径
                summary = getattr(obs, "summary", "")
                file_path = self._extract_path_from_summary(summary)

            if not file_path:
                continue

            # 检查是否有 tracker 记录
            stored_fp = tracker.get(file_path)
            if not stored_fp:
                continue  # 没有历史记录 → 无法判定

            # 读取当前文件并检查
            is_stale, current_fp = tracker.check_from_file(file_path)
            if is_stale:
                stale.append(StaleObservation(
                    observation_id=getattr(obs, "observation_id", ""),
                    file_path=file_path,
                    reason=f"File {file_path} has changed (fingerprint mismatch).",
                ))

        return stale

    def remove_stale(self, wm: WorkingMemory, stale_obs: list[StaleObservation]) -> int:
        """从 working memory 中移除陈旧的 observations。

        通过 observation_id 过滤，避免索引偏移导致的竞态条件。

        Returns:
            移除数量。
        """
        if not stale_obs:
            return 0

        stale_ids = {so.observation_id for so in stale_obs}
        observations = getattr(wm, "recent_observations", [])
        original_count = len(observations)
        wm.recent_observations = [
            obs for obs in observations
            if getattr(obs, "observation_id", "") not in stale_ids
        ]
        return original_count - len(wm.recent_observations)

    # -------------------------------------------------------------------------
    # 内部工具
    # -------------------------------------------------------------------------

    @staticmethod
    def _extract_path_from_summary(summary: str) -> str:
        """从观察摘要中提取文件路径（委托到 memory_utils）。"""
        return extract_path_from_observation(summary)
