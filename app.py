"""Subtitle localization quality-control and delivery service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "subtitle_qc.db"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    def __init__(self, message: str, status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        # 冲突等场景需要把赢家数据和调用方输入回传给客户端，便于保留输入并排解冲突。
        self.details = details


class Database:
    def __init__(self, path: str | os.PathLike[str] = DEFAULT_DB):
        self.path = str(path)
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_language TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL CHECK(duration_ms > 0),
                    owner TEXT NOT NULL,
                    media_name TEXT NOT NULL,
                    media_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
                    mapping_revision INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,language,version_no)
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    user TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('translator','timeline','reviewer')),
                    assigned_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,user,role)
                );
                CREATE TABLE IF NOT EXISTS cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    mapping_state TEXT NOT NULL DEFAULT 'pending',
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(version_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER REFERENCES cues(id) ON DELETE SET NULL,
                    user TEXT NOT NULL,
                    time_ms INTEGER NOT NULL CHECK(time_ms >= 0),
                    body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS glossaries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    source_term TEXT NOT NULL,
                    required_translation TEXT NOT NULL,
                    forbidden_terms TEXT NOT NULL DEFAULT '[]',
                    notes TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(project_id,source_term)
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    reviewer TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL UNIQUE REFERENCES versions(id),
                    supersedes_version_id INTEGER REFERENCES versions(id),
                    snapshot_hash TEXT NOT NULL,
                    manifest TEXT NOT NULL,
                    delivered_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mapping_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('pending','complete')),
                    base_mapping_revision INTEGER NOT NULL,
                    idempotency_key TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS cue_mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES mapping_batches(id) ON DELETE CASCADE,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    cue_id INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    source_start_index INTEGER NOT NULL,
                    source_end_index INTEGER NOT NULL,
                    source_revisions TEXT NOT NULL,
                    UNIQUE(batch_id,ordinal)
                );
                CREATE INDEX IF NOT EXISTS idx_cue_mappings_version ON cue_mappings(version_id);
                CREATE INDEX IF NOT EXISTS idx_mapping_batches_version ON mapping_batches(version_id);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade databases created before the mapping feature."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(versions)")}
        if "mapping_revision" not in cols:
            conn.execute("ALTER TABLE versions ADD COLUMN mapping_revision INTEGER NOT NULL DEFAULT 0")
        cue_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cues)")}
        if "mapping_state" not in cue_cols:
            # 旧数据迁移：缺映射的译文字幕先待确认，确认前不进入复核或交付。
            conn.execute("ALTER TABLE cues ADD COLUMN mapping_state TEXT NOT NULL DEFAULT 'pending'")

    def _audit(self, conn: sqlite3.Connection, actor: str, action: str, entity_type: str,
               entity_id: int | None, details: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(actor,action,entity_type,entity_id,details,created_at) VALUES(?,?,?,?,?,?)",
            (actor, action, entity_type, entity_id, json.dumps(details, ensure_ascii=False), utcnow()),
        )

    def create_project(self, actor: str, payload: dict[str, Any], role: str = "owner") -> dict[str, Any]:
        if role not in {"owner", "admin"}:
            raise DomainError("只有项目负责人可以创建项目", 403)
        name = str(payload.get("name", "")).strip()
        source_language = str(payload.get("source_language", "")).strip()
        media_name = str(payload.get("media_name", "")).strip()
        media_sha = str(payload.get("media_sha256", "")).lower()
        try:
            duration_ms = int(payload.get("duration_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("成片时长必须是毫秒整数") from exc
        if not name or not source_language or not media_name or duration_ms <= 0 or len(media_sha) != 64:
            raise DomainError("项目名称、源语言、成片、时长或校验值不完整")
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO projects(name,source_language,duration_ms,owner,media_name,media_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (name, source_language, duration_ms, actor, media_name, media_sha, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("项目名称已存在", 409) from exc
            self._audit(conn, actor, "project.created", "project", cur.lastrowid, {"name": name})
            return dict(conn.execute("SELECT * FROM projects WHERE id=?", (cur.lastrowid,)).fetchone())

    def set_glossary(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护术语表", 403)
            source_term = str(payload.get("source_term", "")).strip()
            required = str(payload.get("required_translation", "")).strip()
            forbidden = payload.get("forbidden_terms", [])
            if not source_term or not required or not isinstance(forbidden, list):
                raise DomainError("术语、指定译法和禁用词格式不合法")
            conn.execute(
                """INSERT INTO glossaries(project_id,source_term,required_translation,forbidden_terms,notes,created_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,source_term) DO UPDATE SET
                   required_translation=excluded.required_translation,forbidden_terms=excluded.forbidden_terms,notes=excluded.notes""",
                (project_id, source_term, required, json.dumps(forbidden, ensure_ascii=False), str(payload.get("notes", "")), utcnow()),
            )
            self._audit(conn, actor, "glossary.saved", "project", project_id, {"source_term": source_term})
        return {"project_id": project_id, "source_term": source_term, "required_translation": required, "forbidden_terms": forbidden}

    def create_version(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        language = str(payload.get("language", "")).strip()
        if not language:
            raise DomainError("目标语言不能为空")
        parent_id = payload.get("parent_id")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以创建版本", 403)
            if parent_id is not None:
                parent = conn.execute("SELECT * FROM versions WHERE id=? AND project_id=?", (int(parent_id), project_id)).fetchone()
                if not parent or parent["language"] != language:
                    raise DomainError("父版本不存在或目标语言不一致", 409)
            next_no = int(conn.execute("SELECT COALESCE(MAX(version_no),0)+1 value FROM versions WHERE project_id=? AND language=?", (project_id, language)).fetchone()["value"])
            cur = conn.execute(
                "INSERT INTO versions(project_id,language,version_no,parent_id,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (project_id, language, next_no, parent_id, actor, utcnow(), utcnow()),
            )
            self._audit(conn, actor, "version.created", "version", cur.lastrowid, {"language": language, "version_no": next_no})
            return dict(conn.execute("SELECT * FROM versions WHERE id=?", (cur.lastrowid,)).fetchone())

    def assign(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        user = str(payload.get("user", "")).strip()
        assignment_role = str(payload.get("role", "")).strip()
        if not user or assignment_role not in {"translator", "timeline", "reviewer"}:
            raise DomainError("人员或角色不合法")
        with self.connect() as conn:
            version = conn.execute("SELECT v.*,p.owner FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
            if not version:
                raise DomainError("版本不存在", 404)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以分配人员", 403)
            conn.execute("INSERT OR IGNORE INTO assignments(version_id,user,role,assigned_by,created_at) VALUES(?,?,?,?,?)", (version_id, user, assignment_role, actor, utcnow()))
            self._audit(conn, actor, "assignment.saved", "version", version_id, {"user": user, "role": assignment_role})
        return {"version_id": version_id, "user": user, "role": assignment_role}

    def _version(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.id=?", (version_id,)).fetchone()
        if not row:
            raise DomainError("字幕版本不存在", 404)
        return row

    def _can_edit(self, conn: sqlite3.Connection, version: sqlite3.Row, actor: str) -> bool:
        if actor == version["owner"]:
            return True
        return bool(conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role IN ('translator','timeline')", (version["id"], actor)).fetchone())

    def _validate_glossary(self, conn: sqlite3.Connection, project_id: int, text: str) -> None:
        for row in conn.execute("SELECT * FROM glossaries WHERE project_id=?", (project_id,)):
            forbidden = json.loads(row["forbidden_terms"])
            for term in forbidden:
                if term and term in text:
                    raise DomainError(f"字幕包含禁用译法: {term}")
            # The glossary is enforced only when the corresponding source term
            # appears in the localized cue. This keeps it useful without making
            # every cue repeat every glossary word.
            if row["source_term"] in text and row["required_translation"] not in text:
                raise DomainError(f"术语 {row['source_term']} 必须使用指定译法 {row['required_translation']}")

    def save_cue(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改字幕", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(version["revision"]):
                raise DomainError("版本已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(version["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            self._validate_glossary(conn, int(version["project_id"]), text)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone()
                if not existing:
                    raise DomainError("字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM cues WHERE version_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (version_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM cues WHERE version_id=? AND cue_index=? AND id<>?", (version_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("字幕序号已被使用", 409)
            if existing:
                conn.execute("UPDATE cues SET cue_index=?,start_ms=?,end_ms=?,text=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO cues(version_id,cue_index,start_ms,end_ms,text,updated_by,updated_at) VALUES(?,?,?,?,?,?,?)", (version_id, cue_index, start_ms, end_ms, text, actor, utcnow()))
                saved_id = cur.lastrowid
            revision = int(version["revision"]) + 1
            conn.execute("UPDATE versions SET revision=?,updated_at=? WHERE id=?", (revision, utcnow(), version_id))
            self._audit(conn, actor, "cue.saved", "version", version_id, {"cue_id": saved_id, "revision": revision})
        return dict(conn.execute("SELECT * FROM cues WHERE id=?", (saved_id,)).fetchone()) | {"version_revision": revision}

    # ------------------------------------------------------------------
    # 原文字幕与原文↔译文对应关系（映射批次）
    # ------------------------------------------------------------------

    def list_source_cues(self, project_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise DomainError("项目不存在", 404)
            return [dict(r) for r in conn.execute("SELECT * FROM source_cues WHERE project_id=? ORDER BY cue_index", (project_id,))]

    def save_source_cue(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        """新增或修改原文字幕。文本变化会抬高该条 revision，使覆盖它的映射失效。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护原文字幕", 403)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(project["duration_ms"]) or not text:
                raise DomainError("原文字幕时间、序号或内容不合法")
            overlap = conn.execute(
                "SELECT * FROM source_cues WHERE project_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (project_id, int(payload.get("cue_id") or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("原文字幕时间轴发生重叠", 409)
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM source_cues WHERE id=? AND project_id=?", (int(cue_id), project_id)).fetchone()
                if not existing:
                    raise DomainError("原文字幕条目不存在", 404)
                if cue_index != int(existing["cue_index"]):
                    raise DomainError("已有原文字幕不能改序号；序号变化请新增条目并重建映射", 409)
            else:
                if conn.execute("SELECT 1 FROM source_cues WHERE project_id=? AND cue_index=?", (project_id, cue_index)).fetchone():
                    raise DomainError("原文字幕序号已被使用", 409)
            if existing:
                # 只有原文文本变化才抬高修订号；时间轴微调不令译文对应关系失效。
                new_revision = int(existing["revision"]) + (1 if text != existing["text"] else 0)
                conn.execute(
                    "UPDATE source_cues SET start_ms=?,end_ms=?,text=?,revision=?,updated_by=?,updated_at=? WHERE id=?",
                    (start_ms, end_ms, text, new_revision, actor, utcnow(), existing["id"]),
                )
                saved_id, revision = int(existing["id"]), new_revision
            else:
                cur = conn.execute(
                    "INSERT INTO source_cues(project_id,cue_index,start_ms,end_ms,text,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project_id, cue_index, start_ms, end_ms, text, 1, actor, utcnow()),
                )
                saved_id, revision = int(cur.lastrowid), 1
            affected = self._invalidate_for_source_change(conn, project_id, cue_index, existing is None)
            self._audit(conn, actor, "source_cue.saved", "project", project_id,
                        {"cue_id": saved_id, "revision": revision, "affected_versions": affected})
        return dict(conn.execute("SELECT * FROM source_cues WHERE id=?", (saved_id,)).fetchone()) | {"affected_versions": affected}

    def _invalidate_for_source_change(self, conn: sqlite3.Connection, project_id: int, cue_index: int, is_new: bool) -> list[int]:
        """原文一变：覆盖该原文条目的映射失效（派生状态），对应版本的复核结论随之失效；其他句子照旧。"""
        affected: set[int] = set()
        if is_new:
            # 新增原文条导致没有任何批次能覆盖它，所有含译文的版本都失去完整对应。
            rows = conn.execute("SELECT v.id FROM versions v WHERE v.project_id=?", (project_id,)).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT v.id FROM versions v
                JOIN cue_mappings cm ON cm.version_id = v.id
                WHERE v.project_id=? AND cm.source_start_index<=? AND cm.source_end_index>=?
                """,
                (project_id, cue_index, cue_index),
            ).fetchall()
        for row in rows:
            vid = int(row["id"])
            affected.add(vid)
            # 已经在复核或已批准的版本退回草稿：受影响译文和复核结果失效；未受影响的映射仍是 confirmed。
            conn.execute(
                "UPDATE versions SET status='draft',updated_at=? WHERE id=? AND status IN ('review','approved')",
                (utcnow(), vid),
            )
        return sorted(affected)

    def _active_batch(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM mapping_batches WHERE version_id=? AND status='complete' ORDER BY id DESC LIMIT 1",
            (version_id,),
        ).fetchone()

    def _alignment(self, conn: sqlite3.Connection, version: sqlite3.Row, batch: sqlite3.Row | None = None) -> dict[str, Any]:
        """计算译文版与当前原文字幕的对应状况；所有详情、复核队列和交付快照都认这份结果。"""
        project_id = int(version["project_id"])
        source = [dict(r) for r in conn.execute("SELECT * FROM source_cues WHERE project_id=? ORDER BY cue_index", (project_id,))]
        target = [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version["id"],))]
        source_by_id = {s["id"]: s for s in source}
        target_by_id = {t["id"]: t for t in target}
        if batch is None:
            batch = self._active_batch(conn, int(version["id"]))
        mappings: list[dict[str, Any]] = []
        covered_source: set[int] = set()
        mapped_cues: set[int] = set()
        if batch:
            rows = conn.execute("SELECT * FROM cue_mappings WHERE batch_id=? ORDER BY ordinal", (batch["id"],)).fetchall()
            for row in rows:
                src_rows = conn.execute(
                    "SELECT id,revision FROM source_cues WHERE project_id=? AND cue_index BETWEEN ? AND ? ORDER BY cue_index",
                    (project_id, row["source_start_index"], row["source_end_index"]),
                ).fetchall()
                current = {s["id"]: int(s["revision"]) for s in src_rows}
                basis = {int(sid): int(rev) for sid, rev in json.loads(row["source_revisions"])}
                stale_cue_ids = sorted(sid for sid, rev in current.items() if basis.get(sid) != rev)
                missing_basis = sorted(sid for sid in basis if sid not in current)
                stale = bool(stale_cue_ids or missing_basis)
                cue = target_by_id.get(int(row["cue_id"]))
                state = "stale" if stale else "confirmed"
                mappings.append({
                    "cue_id": int(row["cue_id"]),
                    "cue_index": cue["cue_index"] if cue else None,
                    "source_start_index": int(row["source_start_index"]),
                    "source_end_index": int(row["source_end_index"]),
                    "source_cue_ids": [s["id"] for s in src_rows],
                    "state": state,
                    "stale_source_cue_ids": stale_cue_ids + missing_basis,
                })
                covered_source.update(range(int(row["source_start_index"]), int(row["source_end_index"]) + 1))
                mapped_cues.add(int(row["cue_id"]))
        # 缺映射的译文字幕（含旧数据迁移出来的条目）先待确认。
        pending_cue_ids = sorted(t["id"] for t in target if t["id"] not in mapped_cues)
        # 未被任何映射覆盖的原文段（留空检测，基于序号位置）。
        uncovered: list[tuple[int, int]] = []
        if source:
            run_start = prev = source[0]["cue_index"]
            for s in source[1:]:
                idx = s["cue_index"]
                if idx == prev + 1:
                    prev = idx
                    continue
                for x in range(run_start, prev + 1):
                    if x not in covered_source:
                        uncovered.append((x, x))
                run_start = prev = idx
            for x in range(run_start, prev + 1):
                if x not in covered_source:
                    uncovered.append((x, x))
            uncovered.sort()
            merged: list[tuple[int, int]] = []
            for start, end in uncovered:
                if merged and start == merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], end)
                else:
                    merged.append((start, end))
            uncovered = merged
        stale_count = sum(1 for m in mappings if m["state"] == "stale")
        complete = bool(batch and source and target and not pending_cue_ids and not uncovered and stale_count == 0)
        return {
            "batch_id": int(batch["id"]) if batch else None,
            "batch_status": batch["status"] if batch else None,
            "base_mapping_revision": int(batch["base_mapping_revision"]) if batch else None,
            "mapping_revision": int(version["mapping_revision"]),
            "complete": complete,
            "mappings": mappings,
            "pending_cue_ids": pending_cue_ids,
            "uncovered_source_ranges": [list(r) for r in uncovered],
            "stale_count": stale_count,
        }

    def _require_alignment(self, conn: sqlite3.Connection, version: sqlite3.Row, action_label: str) -> dict[str, Any]:
        alignment = self._alignment(conn, version)
        if not alignment["batch_id"]:
            raise DomainError(f"{action_label}前必须先建立原文↔译文映射", 409, {"alignment": alignment})
        if alignment["pending_cue_ids"]:
            raise DomainError(f"{action_label}前仍有待确认的译文字幕（缺映射）", 409, {"alignment": alignment})
        if alignment["uncovered_source_ranges"]:
            raise DomainError(f"{action_label}前映射未覆盖全部原文字幕（存在留空）", 409, {"alignment": alignment})
        if alignment["stale_count"]:
            raise DomainError(f"{action_label}前部分映射依据的原文已修订，请重新确认", 409, {"alignment": alignment})
        return alignment

    def list_mappings(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            alignment = self._alignment(conn, version)
            pending_batch = conn.execute(
                "SELECT * FROM mapping_batches WHERE version_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
                (version_id,),
            ).fetchone()
            alignment["recoverable_pending_batch_id"] = int(pending_batch["id"]) if pending_batch else None
            return alignment

    def review_queue(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            alignment = self._alignment(conn, version)
            mapped_ids = {m["cue_id"] for m in alignment["mappings"]}
            pending_set = set(alignment["pending_cue_ids"])
            all_cues = [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            pending = [c for c in all_cues if c["id"] in pending_set or c["id"] not in mapped_ids]
            stale = [m for m in alignment["mappings"] if m["state"] == "stale"]
            reviewable = [m for m in alignment["mappings"] if m["state"] == "confirmed"]
            return {
                "version_id": version_id,
                "status": version["status"],
                "ready_for_review": alignment["complete"],
                "reviewable_cue_ids": [m["cue_id"] for m in reviewable],
                "stale_mappings": stale,
                "pending_cues": pending,
                "uncovered_source_ranges": alignment["uncovered_source_ranges"],
            }

    def submit_mappings(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        """提交一份完整映射批次：覆盖连续原文段，彼此不交叉、不留空，逐条记下依据的原文修订。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] not in {"draft", "review"}:
                raise DomainError("只有草稿或复核中的版本可以提交映射", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            idem_key = payload.get("idempotency_key")
            if idem_key is not None:
                idem_key = str(idem_key)
            # 重复提交不重复入库：同一幂等键直接返回首个批次（无论后来修订如何变化）。
            if idem_key:
                dup = conn.execute("SELECT * FROM mapping_batches WHERE version_id=? AND idempotency_key=?", (version_id, idem_key)).fetchone()
                if dup:
                    alignment = self._alignment(conn, version, dup)
                    return {"idempotent_replay": True, "batch_id": int(dup["id"]), "status": dup["status"],
                            "mapping_revision": int(version["mapping_revision"]), "alignment": alignment}
            expected = payload.get("expected_mapping_revision")
            if expected is not None and int(expected) != int(version["mapping_revision"]):
                winner = self._alignment(conn, version)
                raise DomainError(
                    "映射批次已被其他成员更新，先到的版本生效；请基于当前对应关系合并后重试",
                    409,
                    {"conflict": True, "your_input": payload.get("mappings"),
                     "current_mapping_revision": int(version["mapping_revision"]),
                     "submitted_mapping_revision": int(expected), "current_alignment": winner},
                )
            items = payload.get("mappings")
            if not isinstance(items, list) or not items:
                raise DomainError("mappings 必须是非空列表")
            source = [dict(r) for r in conn.execute("SELECT * FROM source_cues WHERE project_id=? ORDER BY cue_index", (version["project_id"],))]
            target = conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()
            if not source:
                raise DomainError("项目还没有原文字幕，无法建立对应")
            if not target:
                raise DomainError("版本还没有译文字幕，无法建立对应")
            target_ids = {int(t["id"]) for t in target}
            prepared: list[dict[str, Any]] = []
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    raise DomainError(f"第 {i + 1} 条映射格式不合法")
                cue_id = item.get("cue_id")
                if cue_id is None or int(cue_id) not in target_ids:
                    raise DomainError(f"第 {i + 1} 条映射引用的译文字幕不存在: {cue_id}", 404)
                try:
                    s_idx, e_idx = int(item["source_start_index"]), int(item["source_end_index"])
                except (TypeError, ValueError) as exc:
                    raise DomainError(f"第 {i + 1} 条映射的原文范围必须是整数") from exc
                if s_idx > e_idx:
                    raise DomainError(f"译文字幕 {cue_id} 映射的原文起点不能大于终点")
                prepared.append({"cue_id": int(cue_id), "start": s_idx, "end": e_idx})
            if len({p["cue_id"] for p in prepared}) != len(prepared):
                raise DomainError("一条译文字幕只能出现一次")
            # 一张译文字幕覆盖连续的一段原文字幕：映射之间不交叉也不留空（按原文顺序逐条相接）。
            prepared.sort(key=lambda p: (p["start"], p["end"]))
            src_order = [s["cue_index"] for s in source]
            cursor = 0
            mapped_cue_order: list[int] = []
            for p in prepared:
                ordered = [s for s in source if p["start"] <= s["cue_index"] <= p["end"]]
                expected_indices = [s["cue_index"] for s in ordered]
                if expected_indices != list(range(p["start"], p["end"] + 1)):
                    raise DomainError(f"译文字幕 {p['cue_id']} 覆盖的原文段不连续（中间有缺失）")
                if cursor >= len(src_order) or p["start"] != src_order[cursor]:
                    raise DomainError("映射之间存在交叉或留空：必须从第一条原文字幕起逐条相接")
                cursor += p["end"] - p["start"] + 1
                mapped_cue_order.append(p["cue_id"])
            if cursor != len(src_order):
                raise DomainError("映射没有覆盖全部原文字幕（末尾留空）")
            if set(mapped_cue_order) != target_ids:
                raise DomainError("映射必须覆盖该版本的全部译文字幕，缺映射的条目请先待确认")
            base_rev = int(version["mapping_revision"])
            try:
                cur = conn.execute(
                    "INSERT INTO mapping_batches(version_id,status,base_mapping_revision,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (version_id, "pending", base_rev, idem_key, actor, utcnow()),
                )
            except sqlite3.IntegrityError as exc:
                # 并发提交相同幂等键：先到的已入库，后到的不重复写入。
                winner = conn.execute("SELECT * FROM mapping_batches WHERE version_id=? AND idempotency_key=?", (version_id, idem_key)).fetchone()
                raise DomainError("相同幂等键的映射批次已先提交，本次不重复入库", 409,
                                  {"idempotent_replay": True, "batch_id": int(winner["id"]) if winner else None}) from exc
            batch_id = int(cur.lastrowid)
            for ordinal, p in enumerate(mapped_cue_order):
                seg = next(x for x in prepared if x["cue_id"] == p)
                basis = [(s["id"], int(s["revision"])) for s in source if seg["start"] <= s["cue_index"] <= seg["end"]]
                conn.execute(
                    "INSERT INTO cue_mappings(batch_id,version_id,cue_id,ordinal,source_start_index,source_end_index,source_revisions) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, version_id, p, ordinal, seg["start"], seg["end"], json.dumps(basis)),
                )
            new_rev = base_rev + 1
            conn.execute("UPDATE mapping_batches SET status='complete' WHERE id=?", (batch_id,))
            conn.execute("UPDATE cues SET mapping_state='confirmed' WHERE version_id=?", (version_id,))
            conn.execute("UPDATE versions SET mapping_revision=?,updated_at=? WHERE id=?", (new_rev, utcnow(), version_id))
            self._audit(conn, actor, "mapping.batch_submitted", "version", version_id,
                        {"batch_id": batch_id, "base_mapping_revision": base_rev, "mapping_revision": new_rev})
            alignment = self._alignment(conn, version)
        return {"batch_id": batch_id, "status": "complete", "mapping_revision": new_rev, "alignment": alignment}

    def recover_mappings(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        """写入失败后清理残留的不完整批次，并从最近完整映射批次恢复。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and not self._can_edit(conn, version, actor) and role != "admin":
                raise DomainError("没有该版本的映射恢复权限", 403)
            pendings = conn.execute("SELECT id FROM mapping_batches WHERE version_id=? AND status='pending'", (version_id,)).fetchall()
            for row in pendings:
                conn.execute("DELETE FROM cue_mappings WHERE batch_id=?", (row["id"],))
                conn.execute("DELETE FROM mapping_batches WHERE id=?", (row["id"],))
            latest = self._active_batch(conn, version_id)
            self._audit(conn, actor, "mapping.recovered", "version", version_id,
                        {"removed_pending_batches": [int(r["id"]) for r in pendings],
                         "restored_batch_id": int(latest["id"]) if latest else None})
            alignment = self._alignment(conn, version, latest)
        return {"removed_pending_batches": [int(r["id"]) for r in pendings],
                "restored_batch_id": int(latest["id"]) if latest else None,
                "mapping_revision": alignment["mapping_revision"], "alignment": alignment}

    def add_comment(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        body = str(payload.get("body", "")).strip()
        try:
            time_ms = int(payload.get("time_ms"))
        except (TypeError, ValueError) as exc:
            raise DomainError("评论时间必须是毫秒整数") from exc
        with self.connect() as conn:
            version = self._version(conn, version_id)
            allowed = actor == version["owner"] or conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=?", (version_id, actor)).fetchone()
            if not allowed:
                raise DomainError("只有项目成员可以评论", 403)
            if not body or time_ms < 0 or time_ms > int(version["duration_ms"]):
                raise DomainError("评论内容或时间点不合法")
            cue_id = payload.get("cue_id")
            if cue_id is not None and not conn.execute("SELECT 1 FROM cues WHERE id=? AND version_id=?", (int(cue_id), version_id)).fetchone():
                raise DomainError("评论关联的字幕不存在", 404)
            cur = conn.execute("INSERT INTO comments(version_id,cue_id,user,time_ms,body,created_at) VALUES(?,?,?,?,?,?)", (version_id, cue_id, actor, time_ms, body, utcnow()))
            self._audit(conn, actor, "comment.added", "version", version_id, {"comment_id": cur.lastrowid, "time_ms": time_ms})
        return {"id": int(cur.lastrowid), "version_id": version_id, "cue_id": cue_id, "user": actor, "time_ms": time_ms, "body": body, "status": "open"}

    def submit(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft" or not self._can_edit(conn, version, actor):
                raise DomainError("只有草稿版本的翻译或时间轴人员可以提交复核", 409)
            if not conn.execute("SELECT 1 FROM cues WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("空版本不能提交复核", 409)
            # 详情、复核队列和交付快照都认这份映射：未确认/失效/留空时不得进入复核。
            self._require_alignment(conn, version, "提交复核")
            conn.execute("UPDATE versions SET status='review',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.submitted", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def review(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        decision = str(payload.get("decision", "")).strip()
        if decision not in {"approve", "reject"}:
            raise DomainError("复核决定必须是 approve 或 reject")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "review":
                raise DomainError("版本当前不在复核阶段", 409)
            assigned = conn.execute("SELECT 1 FROM assignments WHERE version_id=? AND user=? AND role='reviewer'", (version_id, actor)).fetchone()
            if not assigned and actor != version["owner"]:
                raise DomainError("没有该版本的复核权限", 403)
            if actor == version["created_by"]:
                raise DomainError("创建人不能复核自己的版本", 403)
            if decision == "approve":
                # 复核期间原文若被修订，受影响的映射已失效，不能批准。
                self._require_alignment(conn, version, "复核通过")
            conn.execute("INSERT INTO reviews(version_id,reviewer,decision,comment,created_at) VALUES(?,?,?,?,?)", (version_id, actor, decision, str(payload.get("comment", "")), utcnow()))
            status = "approved" if decision == "approve" else "draft"
            conn.execute("UPDATE versions SET status=?,updated_at=? WHERE id=?", (status, utcnow(), version_id))
            self._audit(conn, actor, f"version.{decision}", "version", version_id, {"comment": payload.get("comment", "")})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def lock(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            if version["status"] != "approved":
                raise DomainError("只有已批准版本可以锁定", 409)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以锁定版本", 403)
            conn.execute("UPDATE versions SET status='locked',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.locked", "version", version_id, {})
        return dict(conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone())

    def deliver(self, version_id: int, actor: str, role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if actor != version["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以交付", 403)
            if version["status"] not in {"approved", "locked"}:
                raise DomainError("只有批准或锁定版本可以交付", 409)
            if conn.execute("SELECT 1 FROM deliveries WHERE version_id=?", (version_id,)).fetchone():
                raise DomainError("该版本已经交付，不能用新内容覆盖", 409)
            # 交付快照认这份映射：有待确认/失效/留空的对应关系一律不能交付。
            alignment = self._require_alignment(conn, version, "交付")
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            source_cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text,revision FROM source_cues WHERE project_id=? ORDER BY cue_index", (version["project_id"],))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            manifest = {
                "project_id": version["project_id"], "version_id": version_id,
                "language": version["language"], "version_no": version["version_no"],
                "cues": cues, "source_cues": source_cues, "glossary": glossary,
                "mapping": {
                    "batch_id": alignment["batch_id"],
                    "mapping_revision": alignment["mapping_revision"],
                    "mappings": [
                        {"cue_id": m["cue_id"], "source_start_index": m["source_start_index"],
                         "source_end_index": m["source_end_index"], "source_revision_basis": "frozen"}
                        for m in alignment["mappings"]
                    ],
                },
            }
            snapshot_hash = hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            previous = conn.execute("SELECT id FROM deliveries WHERE version_id IN (SELECT id FROM versions WHERE project_id=? AND language=? AND id<>?) ORDER BY id DESC LIMIT 1", (version["project_id"], version["language"], version_id)).fetchone()
            if previous:
                conn.execute("UPDATE versions SET status='superseded',updated_at=? WHERE id=(SELECT version_id FROM deliveries WHERE id=?)", (utcnow(), previous["id"]))
            cur = conn.execute(
                "INSERT INTO deliveries(version_id,supersedes_version_id,snapshot_hash,manifest,delivered_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_id, previous["id"] if previous else None, snapshot_hash, json.dumps(manifest, ensure_ascii=False, sort_keys=True), actor, utcnow()),
            )
            conn.execute("UPDATE versions SET status='delivered',updated_at=? WHERE id=?", (utcnow(), version_id))
            self._audit(conn, actor, "version.delivered", "version", version_id, {"snapshot_hash": snapshot_hash})
        return dict(conn.execute("SELECT * FROM deliveries WHERE id=?", (cur.lastrowid,)).fetchone())

    def list_projects(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM projects ORDER BY id").fetchall()]

    def list_versions(self, project_id: int | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if project_id:
                rows = conn.execute("SELECT * FROM versions WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM versions ORDER BY id").fetchall()
            return [dict(r) for r in rows]

    def list_cues(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            alignment = self._alignment(conn, version)
            live_state = {m["cue_id"]: m["state"] for m in alignment["mappings"]}
            pending = set(alignment["pending_cue_ids"])
            result = []
            for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)):
                row = dict(r)
                # 详情认这份映射：confirmed / stale 由最新批次派生，无映射则为待确认。
                row["mapping_state"] = live_state.get(row["id"], "pending" if row["id"] in pending else row["mapping_state"])
                result.append(row)
            return result

    def list_comments(self, version_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM comments WHERE version_id=? ORDER BY id", (version_id,)).fetchall()]

    def list_deliveries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]

    def audit(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id DESC").fetchall()]


def seed_demo(db: Database) -> dict[str, int]:
    if db.list_projects():
        return {"project": int(db.list_projects()[0]["id"])}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    db.save_source_cue(project["id"], "alice", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "A seal rests on the ice."}, "owner")
    db.save_source_cue(project["id"], "alice", {"cue_index": 2, "start_ms": 3000, "end_ms": 5000, "text": "The wind grows colder by dusk."}, "owner")
    version = db.create_version(project["id"], "alice", {"language": "zh-CN"}, "owner")
    return {"project": int(project["id"]), "version": int(version["id"])}


class Handler(BaseHTTPRequestHandler):
    db: Database
    server_version = "SubtitleQC/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/index.html"}:
                return self._html()
            if parsed.path == "/api/health":
                return self._send({"ok": True})
            if parsed.path == "/api/projects":
                return self._send({"projects": self.db.list_projects()})
            if parsed.path == "/api/versions":
                return self._send({"versions": self.db.list_versions()})
            if parsed.path == "/api/deliveries":
                return self._send({"deliveries": self.db.list_deliveries()})
            if parsed.path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "source-cues":
                return self._send({"source_cues": self.db.list_source_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send({"cues": self.db.list_cues(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send({"comments": self.db.list_comments(int(parts[2]))})
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "mappings":
                return self._send(self.db.list_mappings(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review-queue":
                return self._send(self.db.review_queue(int(parts[2])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send_error(exc)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "projects"]:
                return self._send(self.db.create_project(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "versions":
                return self._send(self.db.create_version(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "glossary":
                return self._send(self.db.set_glossary(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "source-cues":
                return self._send(self.db.save_source_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "assignments":
                return self._send(self.db.assign(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "cues":
                return self._send(self.db.save_cue(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "mappings":
                return self._send(self.db.submit_mappings(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "recover-mappings":
                return self._send(self.db.recover_mappings(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] in {"submit", "lock", "deliver"}:
                version_id = int(parts[2])
                if parts[3] == "submit":
                    return self._send(self.db.submit(version_id, actor, role))
                if parts[3] == "lock":
                    return self._send(self.db.lock(version_id, actor, role))
                return self._send(self.db.deliver(version_id, actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "review":
                return self._send(self.db.review(int(parts[2]), actor, body, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send_error(exc)

    def _send_error(self, exc: Exception) -> None:
        payload: dict[str, Any] = {"error": str(exc)}
        details = getattr(exc, "details", None)
        if details:
            payload.update(details)
        self._send(payload, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
