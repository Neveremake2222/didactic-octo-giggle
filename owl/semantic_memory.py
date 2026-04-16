"""跨任务长期稳定记忆（Phase 1: SQLite 持久化）。

semantic_memory 和 working_memory 的区别：
  - working_memory 生命周期仅限当前 run
  - semantic_memory 跨任务持久化，可跨 session 复用

Phase 1 核心变化：
  - 从纯内存 dict 升级为 SQLite 后端
  - 自动创建 .owl/memory/semantic-memory.db
  - 支持 WAL 模式提升并发性能
  - 写入失败时优雅回退到内存态
  - 所有方法兼容原有接口
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .memory_utils import tokenize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认参数（来自 refactor plan）
# ---------------------------------------------------------------------------

DEFAULT_DB_PATH = ".owl/memory/semantic-memory.db"
DEFAULT_TABLE_NAME = "semantic_records"
DEFAULT_SEARCH_LIMIT = 8
DEFAULT_IMPORTANCE_SCORE = 0.5
DEFAULT_FRESHNESS_HALFLIFE_HOURS = 168
WRITE_BATCH_SIZE = 32
ENABLE_WAL = True
AUTO_INIT_SCHEMA = True


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SemanticRecord（不变）
# ---------------------------------------------------------------------------


@dataclass
class SemanticRecord:
    """一条长期记忆记录。"""

    # 唯一标识
    record_id: str

    # 记录类型（如 "file_summary", "module_responsibility", "test_convention"）
    category: str

    # 记录正文
    content: str

    # 关联的 repo 路径（如果有）
    repo_path: str = ""

    # 标签（用于检索过滤）
    tags: list[str] = field(default_factory=list)

    # 来源（哪次 run 写入的）
    source_run_id: str = ""

    # 文件关联路径
    file_path: str = ""

    # 文件版本哈希（写入时的 SHA-256）
    file_version: str = ""

    # freshness hash（用于判断是否过期）
    freshness_hash: str = ""

    # Phase 2: 有效性字段
    superseded_by: str = ""       # record_id of replacement
    invalidated_at: str = ""      # ISO timestamp when invalidated
    importance_score: float = 1.0  # 0.0-1.0 for recall ranking

    # 创建时间
    created_at: str = ""

    # 最后更新时间
    updated_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now_iso()
        if not self.updated_at:
            self.updated_at = self.created_at

    # --- Phase 2: 有效性方法 ---

    def invalidate(self) -> None:
        """标记此记录为已失效。"""
        self.invalidated_at = _now_iso()

    def supersede(self, new_record_id: str) -> None:
        """标记此记录已被新记录替代。"""
        self.superseded_by = new_record_id

    def is_active(self) -> bool:
        """此记录是否仍然有效（未被 invalidate 或 supersede）。"""
        return not self.invalidated_at and not self.superseded_by

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "category": self.category,
            "content": self.content,
            "repo_path": self.repo_path,
            "tags": list(self.tags),
            "source_run_id": self.source_run_id,
            "file_path": self.file_path,
            "file_version": self.file_version,
            "freshness_hash": self.freshness_hash,
            "superseded_by": self.superseded_by,
            "invalidated_at": self.invalidated_at,
            "importance_score": self.importance_score,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticRecord:
        return cls(
            record_id=str(data.get("record_id", "")),
            category=str(data.get("category", "")),
            content=str(data.get("content", "")),
            repo_path=str(data.get("repo_path", "")),
            tags=list(data.get("tags", [])),
            source_run_id=str(data.get("source_run_id", "")),
            file_path=str(data.get("file_path", "")),
            file_version=str(data.get("file_version", "")),
            freshness_hash=str(data.get("freshness_hash", "")),
            superseded_by=str(data.get("superseded_by", "")),
            invalidated_at=str(data.get("invalidated_at", "")),
            importance_score=float(data.get("importance_score", 1.0)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )

    def to_db_row(self) -> dict[str, Any]:
        """转换为数据库行字典。"""
        return {
            "record_id": self.record_id,
            "repo_path": self.repo_path,
            "category": self.category,
            "content": self.content,
            "tags_json": json.dumps(self.tags, ensure_ascii=False),
            "source_run_id": self.source_run_id,
            "file_path": self.file_path,
            "file_version": self.file_version,
            "freshness_hash": self.freshness_hash,
            "importance_score": self.importance_score,
            "invalidated_at": self.invalidated_at,
            "superseded_by": self.superseded_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> SemanticRecord:
        """从数据库行恢复 SemanticRecord。"""
        tags_raw = row.get("tags_json", "[]")
        try:
            tags = json.loads(tags_raw)
        except Exception:
            tags = []
        return cls(
            record_id=str(row.get("record_id", "")),
            repo_path=str(row.get("repo_path", "")),
            category=str(row.get("category", "")),
            content=str(row.get("content", "")),
            tags=tags,
            source_run_id=str(row.get("source_run_id", "")),
            file_path=str(row.get("file_path", "")),
            file_version=str(row.get("file_version", "")),
            freshness_hash=str(row.get("freshness_hash", "")),
            importance_score=float(row.get("importance_score", 1.0)),
            invalidated_at=str(row.get("invalidated_at", "")),
            superseded_by=str(row.get("superseded_by", "")),
            created_at=str(row.get("created_at", "")),
            updated_at=str(row.get("updated_at", "")),
        )


# ---------------------------------------------------------------------------
# SQLite 后端
# ---------------------------------------------------------------------------

_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {DEFAULT_TABLE_NAME} (
    record_id        TEXT PRIMARY KEY,
    repo_path        TEXT NOT NULL DEFAULT '',
    category         TEXT NOT NULL DEFAULT '',
    content          TEXT NOT NULL DEFAULT '',
    tags_json        TEXT NOT NULL DEFAULT '[]',
    source_run_id    TEXT NOT NULL DEFAULT '',
    file_path        TEXT NOT NULL DEFAULT '',
    file_version     TEXT NOT NULL DEFAULT '',
    freshness_hash   TEXT NOT NULL DEFAULT '',
    importance_score REAL NOT NULL DEFAULT {DEFAULT_IMPORTANCE_SCORE},
    invalidated_at   TEXT NOT NULL DEFAULT '',
    superseded_by    TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sm_repo_path
    ON {DEFAULT_TABLE_NAME}(repo_path);
CREATE INDEX IF NOT EXISTS idx_sm_category
    ON {DEFAULT_TABLE_NAME}(category);
CREATE INDEX IF NOT EXISTS idx_sm_file_path
    ON {DEFAULT_TABLE_NAME}(file_path);
CREATE INDEX IF NOT EXISTS idx_sm_created_at
    ON {DEFAULT_TABLE_NAME}(created_at);
"""

# 数据库列名列表（与 schema 定义顺序一致）
_DB_COLUMNS = (
    "record_id", "repo_path", "category", "content", "tags_json",
    "source_run_id", "file_path", "file_version", "freshness_hash",
    "importance_score", "invalidated_at", "superseded_by",
    "created_at", "updated_at",
)
_DB_COLUMNS_STR = ", ".join(_DB_COLUMNS)


def _row_to_record(row: tuple) -> SemanticRecord:
    """将 SQLite row tuple convert to SemanticRecord. """
    row_dict = dict(zip(_DB_COLUMNS, row))
    return SemanticRecord.from_db_row(row_dict)


class _SQLiteBackend:
    """SQLite 后端封装。线程安全，支持 WAL。"""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        enable_wal: bool = ENABLE_WAL,
    ):
        self._db_path = db_path
        self._enable_wal = enable_wal
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._init_db()

    def _resolve_db_path(self) -> Path:
        """将相对路径解析为绝对路径。"""
        path = Path(self._db_path)
        if not path.is_absolute():
            # 相对于 cwd
            path = Path.cwd() / path
        return path

    def _init_db(self) -> None:
        """初始化数据库连接和 schema。"""
        db_path = self._resolve_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            conn = sqlite3.connect(str(db_path), timeout=10.0, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            if self._enable_wal:
                conn.execute("PRAGMA wal_autocheckpoint=1000")
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
            self._conn = conn
            logger.debug("SemanticMemory SQLite initialized at %s", db_path)
        except sqlite3.Error as exc:
            logger.warning("Failed to initialize SQLite at %s: %s. Falling back to memory mode.", db_path, exc)
            self._conn = None

    def _ensure_conn(self) -> sqlite3.Connection | None:
        """确保连接有效，失败时尝试重建。"""
        if self._conn is None:
            self._init_db()
        return self._conn

    # --- 写入 ---

    def upsert(self, record: SemanticRecord) -> None:
        """插入或更新一条记录。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return
            row = record.to_db_row()
            # 更新 updated_at
            row["updated_at"] = _now_iso()
            # 检查是否已存在以保留 created_at
            try:
                existing = conn.execute(
                    f"SELECT created_at FROM {DEFAULT_TABLE_NAME} WHERE record_id = ?",
                    (row["record_id"],),
                ).fetchone()
                if existing:
                    row["created_at"] = existing[0]
            except sqlite3.Error:
                pass
            try:
                conn.execute(
                    f"""INSERT INTO {DEFAULT_TABLE_NAME}
                       ({",".join(row.keys())})
                       VALUES ({",".join("?" for _ in row)})
                       ON CONFLICT(record_id) DO UPDATE SET
                       {",".join(f"{k}=excluded.{k}" for k in row if k != "record_id")}""",
                    tuple(row.values()),
                )
                conn.commit()
            except sqlite3.Error as exc:
                logger.warning("Failed to upsert record %s: %s", record.record_id, exc)

    def upsert_many(self, records: list[SemanticRecord]) -> None:
        """批量插入或更新记录。"""
        if not records:
            return
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return
            rows = []
            now = _now_iso()
            for record in records:
                row = record.to_db_row()
                row["updated_at"] = now
                rows.append(row)

            try:
                # 批量 upsert
                conn.execute(f"PRAGMA synchronous=OFF")
                for row in rows:
                    try:
                        existing = conn.execute(
                            f"SELECT created_at FROM {DEFAULT_TABLE_NAME} WHERE record_id = ?",
                            (row["record_id"],),
                        ).fetchone()
                        if existing:
                            row["created_at"] = existing[0]
                    except sqlite3.Error:
                        pass
                    try:
                        conn.execute(
                            f"""INSERT INTO {DEFAULT_TABLE_NAME}
                               ({",".join(row.keys())})
                               VALUES ({",".join("?" for _ in row)})
                               ON CONFLICT(record_id) DO UPDATE SET
                               {",".join(f"{k}=excluded.{k}" for k in row if k != "record_id")}""",
                            tuple(row.values()),
                        )
                    except sqlite3.Error:
                        pass
                conn.commit()
                conn.execute(f"PRAGMA synchronous=NORMAL")
            except sqlite3.Error as exc:
                logger.warning("Failed to batch upsert %d records: %s", len(records), exc)

    def delete(self, record_id: str) -> bool:
        """删除一条记录。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return False
            try:
                cur = conn.execute(
                    f"DELETE FROM {DEFAULT_TABLE_NAME} WHERE record_id = ?",
                    (record_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            except sqlite3.Error as exc:
                logger.warning("Failed to delete record %s: %s", record_id, exc)
                return False

    def invalidate_by_file(self, file_path: str, new_version: str | None = None) -> int:
        """使所有与 file_path 关联的记录失效。返回失效记录数。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return 0
            now = _now_iso()
            try:
                cur = conn.execute(
                    f"""UPDATE {DEFAULT_TABLE_NAME}
                       SET invalidated_at = ?, file_version = COALESCE(NULLIF(?, ''), file_version)
                       WHERE file_path = ? AND invalidated_at = ''""",
                    (now, new_version or "", file_path),
                )
                conn.commit()
                return cur.rowcount
            except sqlite3.Error as exc:
                logger.warning("Failed to invalidate records for %s: %s", file_path, exc)
                return 0

    # --- 查询 ---

    def get(self, record_id: str) -> SemanticRecord | None:
        """按 ID 获取记录。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return None
            try:
                row = conn.execute(
                    f"SELECT {_DB_COLUMNS_STR} FROM {DEFAULT_TABLE_NAME} WHERE record_id = ?",
                    (record_id,),
                ).fetchone()
                if row is None:
                    return None
                return _row_to_record(row)
            except sqlite3.Error as exc:
                logger.warning("Failed to get record %s: %s", record_id, exc)
                return None

    def get_active_by_id(self, record_id: str) -> SemanticRecord | None:
        """按 ID 获取记录，仅返回 active 记录。"""
        record = self.get(record_id)
        if record and record.is_active():
            return record
        return None

    def search(
        self,
        query: str = "",
        repo_path: str | None = None,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        file_path: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[SemanticRecord]:
        """检索 active 记录。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []

            conditions = ["invalidated_at = ''", "superseded_by = ''"]
            params: list[Any] = []

            if repo_path:
                conditions.append("repo_path = ?")
                params.append(repo_path)

            if categories:
                placeholders = ",".join("?" * len(categories))
                conditions.append(f"category IN ({placeholders})")
                params.extend(categories)

            if file_path:
                conditions.append("file_path = ?")
                params.append(file_path)

            where_clause = " AND ".join(conditions)

            tag_set = {str(tag) for tag in (tags or []) if str(tag)}

            # 如果有 query，做 token overlap 过滤
            if query:
                query_tokens = tokenize(query)
                if query_tokens:
                    all_rows = []
                    try:
                        rows = conn.execute(
                            f"SELECT {_DB_COLUMNS_STR} FROM {DEFAULT_TABLE_NAME} WHERE {where_clause}",
                            params,
                        ).fetchall()
                        for row in rows:
                            record = _row_to_record(row)
                            record_tokens = tokenize(record.content)
                            path_tokens = tokenize(record.repo_path)
                            tag_tokens = set()
                            for tag in record.tags:
                                tag_tokens.update(tokenize(tag))
                            if tag_set and not (tag_set & set(record.tags)):
                                continue
                            all_tokens = record_tokens | path_tokens | tag_tokens
                            if query_tokens & all_tokens:
                                all_rows.append(row)
                        # 按 importance 降序 + created_at 降序（_DB_COLUMNS 顺序）
                        all_rows.sort(key=lambda r: (r[9], r[12]), reverse=True)
                        return [_row_to_record(row) for row in all_rows[:limit]]
                    except sqlite3.Error as exc:
                        logger.warning("Search query failed: %s", exc)
                        return []

            try:
                rows = conn.execute(
                    f"""SELECT {_DB_COLUMNS_STR} FROM {DEFAULT_TABLE_NAME}
                       WHERE {where_clause}
                       ORDER BY importance_score DESC, created_at DESC
                       LIMIT ?""",
                    [*params, limit],
                ).fetchall()
                records = [_row_to_record(row) for row in rows]
                if tag_set:
                    records = [record for record in records if tag_set & set(record.tags)]
                return records[:limit]
            except sqlite3.Error as exc:
                logger.warning("Search failed: %s", exc)
                return []

    def all_records(self) -> list[SemanticRecord]:
        """返回所有记录。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return []
            try:
                rows = conn.execute(
                    f"SELECT {_DB_COLUMNS_STR} FROM {DEFAULT_TABLE_NAME} ORDER BY created_at DESC"
                ).fetchall()
                return [_row_to_record(row) for row in rows]
            except sqlite3.Error as exc:
                logger.warning("Failed to get all records: %s", exc)
                return []

    def count(self) -> int:
        """返回 active 记录总数。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return 0
            try:
                cur = conn.execute(
                    f"SELECT COUNT(*) FROM {DEFAULT_TABLE_NAME} WHERE invalidated_at = '' AND superseded_by = ''"
                )
                return cur.fetchone()[0]
            except sqlite3.Error as exc:
                logger.warning("Failed to count records: %s", exc)
                return 0

    def count_total(self) -> int:
        """返回所有记录总数（含已失效）。"""
        with self._lock:
            conn = self._ensure_conn()
            if conn is None:
                return 0
            try:
                cur = conn.execute(f"SELECT COUNT(*) FROM {DEFAULT_TABLE_NAME}")
                return cur.fetchone()[0]
            except sqlite3.Error:
                return 0

    def close(self) -> None:
        """关闭数据库连接（先做 WAL checkpoint 再关闭，防止 Windows 文件锁残留）。"""
        with self._lock:
            if self._conn:
                try:
                    # WAL checkpoint: 将 WAL 内容写回主数据库
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None


# ---------------------------------------------------------------------------
# SemanticMemory（统一入口，SQLite 后端 + 内存回退）
# ---------------------------------------------------------------------------


class SemanticMemory:
    """跨任务长期稳定记忆（Phase 1 SQLite 持久化版）。

    使用方式：
      sm = SemanticMemory()
      sm.put(record)
      results = sm.search(query="auth bug", repo_path="/repo", limit=5)

    降级策略：
      SQLite 初始化失败时，自动回退到纯内存模式，
      不影响主流程运行。内存模式在进程重启后会丢失数据。
    """

    def __init__(
        self,
        records: list[SemanticRecord] | None = None,
        db_path: str | None = None,
        enable_wal: bool = ENABLE_WAL,
    ):
        self._db_path = db_path
        self._enable_wal = enable_wal
        self._use_db = db_path is not None

        # SQLite 后端（仅在显式提供 db_path 时启用）
        self._backend: _SQLiteBackend | None = None
        if db_path is not None:
            try:
                self._backend = _SQLiteBackend(db_path=db_path, enable_wal=enable_wal)
            except Exception as exc:
                logger.warning("SemanticMemory init failed: %s. Using in-memory fallback.", exc)
                self._use_db = False

        # 内存回退：用于 SQLite 不可用时的进程内存储
        self._memory: dict[str, SemanticRecord] = {}

        # 从 records 列表初始化（仅在内存模式或导入时使用）
        if records:
            for record in records:
                self._memory[record.record_id] = record
                if self._backend is not None:
                    self._backend.upsert(record)

    # --- 写入（Plan Phase 1 API） ---

    def put(self, record: SemanticRecord) -> SemanticRecord:
        """写入一条记录。如果 record_id 已存在则更新。

        写入路径：SQLite 后端（主）→ 内存回退（辅）。
        SQLite 写入失败不影响返回值，主流程不崩溃。
        """
        # 先更新内存
        if record.record_id in self._memory:
            existing = self._memory[record.record_id]
            record.created_at = existing.created_at
        record.updated_at = _now_iso()
        self._memory[record.record_id] = record

        # 尝试写 SQLite
        if self._backend is not None:
            try:
                self._backend.upsert(record)
            except Exception as exc:
                logger.warning("SQLite put failed for %s: %s. Continuing with memory-only.", record.record_id, exc)

        return record

    def put_many(self, records: list[SemanticRecord]) -> None:
        """批量写入记录。

        SQLite 批量写入失败时，回退到逐条 put。
        """
        if not records:
            return

        for record in records:
            if record.record_id in self._memory:
                existing = self._memory[record.record_id]
                record.created_at = existing.created_at
            record.updated_at = _now_iso()
            self._memory[record.record_id] = record

        if self._backend is not None:
            try:
                self._backend.upsert_many(records)
            except Exception as exc:
                logger.warning("SQLite batch put failed: %s. Falling back to individual puts.", exc)
                # 回退：逐条重写（用于已从 records 填充过的 memory）
                if self._backend is not None:
                    for record in records:
                        try:
                            self._backend.upsert(record)
                        except Exception:
                            pass

    def delete(self, record_id: str) -> bool:
        """删除一条记录。返回是否成功删除。"""
        found = record_id in self._memory
        if found:
            del self._memory[record_id]
        if self._backend is not None:
            try:
                return self._backend.delete(record_id) or found
            except Exception as exc:
                logger.warning("SQLite delete failed for %s: %s", record_id, exc)
        return found

    # --- 查询（Plan Phase 1 API） ---

    def get(self, record_id: str) -> SemanticRecord | None:
        """按 ID 获取记录。优先查 SQLite。"""
        if self._backend is not None:
            try:
                record = self._backend.get(record_id)
                if record:
                    return record
            except Exception:
                pass
        return self._memory.get(record_id)

    def get_active_by_id(self, record_id: str) -> SemanticRecord | None:
        """按 ID 获取记录，仅返回 active 记录。"""
        record = self.get(record_id)
        if record and record.is_active():
            return record
        return None

    def search(
        self,
        query: str = "",
        category: str | None = None,
        tags: list[str] | None = None,
        repo_path: str | None = None,
        file_path: str | None = None,
        top_k: int = DEFAULT_SEARCH_LIMIT,
    ) -> list[SemanticRecord]:
        """检索记录。

        过滤维度：
          - category  — 精确匹配（单个，兼容旧 API）
          - categories — 精确匹配（多个，Plan Phase 1 API）
          - tags      — 任一 tag 匹配
          - repo_path — 精确匹配
          - file_path — 精确匹配
          - query     — token overlap 匹配
        """
        # 兼容旧 API：category 参数展开为 categories
        categories: list[str] | None = None
        if category:
            categories = [category]

        if self._backend is not None:
            try:
                return self._backend.search(
                    query=query,
                    repo_path=repo_path,
                    categories=categories,
                    tags=tags,
                    file_path=file_path,
                    limit=top_k,
                )
            except Exception as exc:
                logger.warning("SQLite search failed: %s. Falling back to memory search.", exc)

        # 内存回退
        candidates = list(self._memory.values())

        # 只返回 active
        candidates = [r for r in candidates if r.is_active()]

        if repo_path:
            candidates = [r for r in candidates if r.repo_path == repo_path]

        if categories:
            candidates = [r for r in candidates if r.category in categories]

        if file_path:
            candidates = [r for r in candidates if r.file_path == file_path]

        if tags:
            tag_set = set(tags)
            candidates = [r for r in candidates if tag_set & set(r.tags)]

        if query:
            q_tokens = tokenize(query)
            if q_tokens:
                candidates = [
                    r for r in candidates
                    if q_tokens & tokenize(r.content)
                    or q_tokens & tokenize(r.repo_path)
                    or q_tokens & set(t for tag in r.tags for t in tokenize(tag))
                ]

        # 按 importance + created_at 排序
        candidates.sort(key=lambda r: (r.importance_score, r.created_at), reverse=True)
        return candidates[:top_k]

    def invalidate_by_file(self, file_path: str, new_version: str | None = None) -> int:
        """使所有与 file_path 关联的记录失效。返回失效记录数。

        Plan Phase 1 API。
        """
        invalidated = 0

        # 内存回退：标记 active 记录
        for record in self._memory.values():
            if record.file_path == file_path and record.is_active():
                record.invalidate()
                invalidated += 1

        # SQLite
        if self._backend is not None:
            try:
                db_count = self._backend.invalidate_by_file(file_path, new_version)
                return max(invalidated, db_count)
            except Exception as exc:
                logger.warning("SQLite invalidate_by_file failed: %s", exc)

        return invalidated

    def all_records(self) -> list[SemanticRecord]:
        """返回所有记录。"""
        if self._backend is not None:
            try:
                return self._backend.all_records()
            except Exception:
                pass
        return list(self._memory.values())

    def count(self) -> int:
        """返回 active 记录总数。"""
        if self._backend is not None:
            try:
                return self._backend.count()
            except Exception:
                pass
        return sum(1 for r in self._memory.values() if r.is_active())

    # --- 序列化（兼容旧 API） ---

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self._memory.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SemanticMemory:
        records = [
            SemanticRecord.from_dict(r)
            for r in data.get("records", [])
        ]
        # 纯内存模式（用于反序列化，不创建 SQLite）
        instance = cls(records=records)
        return instance

    @staticmethod
    def make_record_id(category: str, key: str) -> str:
        """生成一个稳定的 record_id。"""
        raw = f"{category}:{key}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def __len__(self) -> int:
        return self.count()

    def __contains__(self, record_id: str) -> bool:
        return self.get(record_id) is not None

    def close(self) -> None:
        """关闭底层数据库连接。"""
        if self._backend:
            try:
                self._backend.close()
            except Exception:
                pass

    def __del__(self) -> None:
        """析构时关闭连接，防止 Windows SQLite WAL 文件锁残留。"""
        try:
            self.close()
        except Exception:
            pass
