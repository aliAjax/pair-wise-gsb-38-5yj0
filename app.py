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
    def __init__(self, message: str, status: int = 400, extra: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


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
                    source_revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id),
                    language TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    parent_id INTEGER REFERENCES versions(id),
                    status TEXT NOT NULL DEFAULT 'draft',
                    revision INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_cues (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    cue_index INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
                    end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
                    text TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    updated_by TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,cue_index)
                );
                CREATE TABLE IF NOT EXISTS mapping_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
                    batch_no INTEGER NOT NULL,
                    source_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    idempotency_key TEXT,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(version_id,batch_no),
                    UNIQUE(version_id,idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS mappings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES mapping_batches(id) ON DELETE CASCADE,
                    cue_id INTEGER NOT NULL REFERENCES cues(id) ON DELETE CASCADE,
                    source_start_index INTEGER NOT NULL,
                    source_end_index INTEGER NOT NULL,
                    source_revision INTEGER NOT NULL,
                    CHECK(source_end_index >= source_start_index)
                );
                """
            )
            # 旧库迁移：projects 表补充原文修订号
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
            if "source_revision" not in cols:
                conn.execute("ALTER TABLE projects ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 0")

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
    # 原文字幕与原文-译文映射
    # ------------------------------------------------------------------

    def list_source_cues(self, project_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM source_cues WHERE project_id=? ORDER BY cue_index", (project_id,)).fetchall()]

    def _source_cues(self, conn: sqlite3.Connection, project_id: int) -> dict[int, dict[str, Any]]:
        return {r["cue_index"]: dict(r) for r in conn.execute("SELECT * FROM source_cues WHERE project_id=?", (project_id,)).fetchall()}

    def _active_batch(self, conn: sqlite3.Connection, version_id: int) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM mapping_batches WHERE version_id=? AND status='active' ORDER BY batch_no DESC LIMIT 1", (version_id,)).fetchone()

    def save_source_cue(self, project_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if not project:
                raise DomainError("项目不存在", 404)
            if actor != project["owner"] and role != "admin":
                raise DomainError("只有项目负责人可以维护原文字幕", 403)
            expected = payload.get("expected_revision")
            if expected is not None and int(expected) != int(project["source_revision"]):
                raise DomainError("原文字幕已被其他成员修改，请刷新后重试", 409)
            try:
                cue_index = int(payload.get("cue_index"))
                start_ms = int(payload.get("start_ms"))
                end_ms = int(payload.get("end_ms"))
            except (TypeError, ValueError) as exc:
                raise DomainError("字幕序号和时间必须是整数") from exc
            text = str(payload.get("text", "")).strip()
            if cue_index < 0 or start_ms < 0 or end_ms <= start_ms or end_ms > int(project["duration_ms"]) or not text:
                raise DomainError("字幕时间、序号或内容不合法")
            cue_id = payload.get("cue_id")
            existing = None
            if cue_id is not None:
                existing = conn.execute("SELECT * FROM source_cues WHERE id=? AND project_id=?", (int(cue_id), project_id)).fetchone()
                if not existing:
                    raise DomainError("原文字幕条目不存在", 404)
            overlap = conn.execute(
                "SELECT * FROM source_cues WHERE project_id=? AND id<>? AND start_ms<? AND end_ms>? LIMIT 1",
                (project_id, int(cue_id or -1), end_ms, start_ms),
            ).fetchone()
            if overlap:
                raise DomainError("原文字幕时间轴发生重叠", 409)
            index_owner = conn.execute("SELECT * FROM source_cues WHERE project_id=? AND cue_index=? AND id<>?", (project_id, cue_index, int(cue_id or -1))).fetchone()
            if index_owner:
                raise DomainError("原文字幕序号已被使用", 409)
            revision = int(project["source_revision"]) + 1
            if existing:
                conn.execute("UPDATE source_cues SET cue_index=?,start_ms=?,end_ms=?,text=?,revision=?,updated_by=?,updated_at=? WHERE id=?", (cue_index, start_ms, end_ms, text, revision, actor, utcnow(), existing["id"]))
                saved_id = existing["id"]
            else:
                cur = conn.execute("INSERT INTO source_cues(project_id,cue_index,start_ms,end_ms,text,revision,updated_by,updated_at) VALUES(?,?,?,?,?,?,?,?)", (project_id, cue_index, start_ms, end_ms, text, revision, actor, utcnow()))
                saved_id = cur.lastrowid
            conn.execute("UPDATE projects SET source_revision=? WHERE id=?", (revision, project_id))
            self._audit(conn, actor, "source_cue.saved", "project", project_id, {"cue_id": saved_id, "source_revision": revision})
            invalidated = self._invalidate_versions(conn, actor, project_id, revision)
        return dict(conn.execute("SELECT * FROM source_cues WHERE id=?", (saved_id,)).fetchone()) | {"source_revision": revision, "invalidated_versions": invalidated}

    def _invalidate_versions(self, conn: sqlite3.Connection, actor: str, project_id: int, source_revision: int) -> list[int]:
        """原文变更后，把映射已失效的在审/已批准/已锁定版本退回草稿。"""
        invalidated = []
        rows = conn.execute(
            "SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.project_id=? AND v.status IN ('review','approved','locked')",
            (project_id,),
        ).fetchall()
        for v in rows:
            health = self._mapping_health(conn, v)
            summary = health["summary"]
            if summary["pending"] or summary["stale"] or not summary["complete"]:
                affected = [c["id"] for c in health["items"] if c["mapping_status"] != "fresh"]
                conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), v["id"]))
                self._audit(conn, actor, "version.review_invalidated", "version", v["id"], {"source_revision": source_revision, "affected_cues": affected})
                invalidated.append(v["id"])
        return invalidated

    def _mapping_health(self, conn: sqlite3.Connection, version: sqlite3.Row) -> dict[str, Any]:
        """按当前原文计算每条译文字幕的映射状态：fresh / stale / pending。"""
        cues = [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version["id"],)).fetchall()]
        source = self._source_cues(conn, version["project_id"])
        batch = self._active_batch(conn, version["id"])
        entries: dict[int, dict[str, Any]] = {}
        if batch:
            for m in conn.execute("SELECT * FROM mappings WHERE batch_id=?", (batch["id"],)).fetchall():
                entries[m["cue_id"]] = dict(m)
        items = []
        counts = {"fresh": 0, "stale": 0, "pending": 0}
        covered: set[int] = set()
        ordered = True
        last_end: int | None = None
        for cue in cues:
            m = entries.get(cue["id"])
            status = "pending"
            info = None
            if m:
                seg = [source[i] for i in sorted(source) if m["source_start_index"] <= i <= m["source_end_index"]]
                covered.update(s["cue_index"] for s in seg)
                if last_end is not None and m["source_start_index"] <= last_end:
                    ordered = False
                last_end = max(last_end if last_end is not None else m["source_end_index"], m["source_end_index"])
                stale = not seg or any(s["revision"] > m["source_revision"] for s in seg)
                status = "stale" if stale else "fresh"
                info = {
                    "source_start_index": m["source_start_index"],
                    "source_end_index": m["source_end_index"],
                    "source_revision": m["source_revision"],
                    "source_text": " ".join(s["text"] for s in seg),
                }
            counts[status] += 1
            items.append(cue | {"mapping": info, "mapping_status": status})
        complete = bool(batch) and bool(source) and ordered and covered == set(source)
        source_revision = conn.execute("SELECT source_revision FROM projects WHERE id=?", (version["project_id"],)).fetchone()["source_revision"]
        summary = {"total": len(cues), **counts, "complete": complete, "source_revision": source_revision}
        return {"batch": dict(batch) if batch else None, "items": items, "summary": summary}

    def _batch_payload(self, conn: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
        entries = [dict(r) for r in conn.execute(
            "SELECT m.*,c.cue_index FROM mappings m JOIN cues c ON c.id=m.cue_id WHERE m.batch_id=? ORDER BY c.cue_index",
            (batch["id"],),
        ).fetchall()]
        return {"batch": dict(batch), "entries": entries}

    def save_mapping_batch(self, version_id: int, actor: str, payload: dict[str, Any], role: str = "viewer") -> dict[str, Any]:
        mappings_in = payload.get("mappings")
        if not isinstance(mappings_in, list) or not mappings_in:
            raise DomainError("映射批次不能为空")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = self._version(conn, version_id)
            if version["status"] != "draft":
                raise DomainError("只有草稿版本可以修改映射", 409)
            if not self._can_edit(conn, version, actor):
                raise DomainError("没有该版本的翻译或时间轴权限", 403)
            entries = []
            for raw in mappings_in:
                try:
                    entries.append({
                        "cue_id": int(raw.get("cue_id")),
                        "source_start_index": int(raw.get("source_start_index")),
                        "source_end_index": int(raw.get("source_end_index")),
                    })
                except (TypeError, ValueError, AttributeError) as exc:
                    raise DomainError("映射条目必须包含整数 cue_id、source_start_index、source_end_index") from exc
            content_hash = hashlib.sha256(json.dumps(sorted(entries, key=lambda e: e["cue_id"]), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            idem = payload.get("idempotency_key")
            idem = str(idem).strip() if idem is not None else ""
            current = self._active_batch(conn, version_id)
            source_revision = int(conn.execute("SELECT source_revision FROM projects WHERE id=?", (version["project_id"],)).fetchone()["source_revision"])
            # 重复提交不重复入库：幂等键或内容相同的重试直接返回已入库批次；
            # 但原文已变更时，相同区间是有意义的重新确认，必须按新原文修订入库
            if idem:
                dup = conn.execute("SELECT * FROM mapping_batches WHERE version_id=? AND idempotency_key=?", (version_id, idem)).fetchone()
                if dup:
                    return self._batch_payload(conn, dup) | {"duplicate": True}
            if current and current["content_hash"] == content_hash and int(current["source_revision"]) == source_revision:
                return self._batch_payload(conn, current) | {"duplicate": True}
            try:
                base = int(payload.get("base_batch_no") or 0)
            except (TypeError, ValueError) as exc:
                raise DomainError("base_batch_no 必须是整数") from exc
            current_no = int(current["batch_no"]) if current else 0
            if base != current_no:
                # 两人同时提交同一批映射：先到的生效，后到的保留输入并看到冲突
                raise DomainError(
                    "映射批次已被其他成员更新，请基于最新批次重新提交", 409,
                    extra={
                        "current_batch": self._batch_payload(conn, current) if current else None,
                        "your_input": {"base_batch_no": base, "mappings": entries},
                    },
                )
            cues = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=?", (version_id,)).fetchall()}
            source = self._source_cues(conn, version["project_id"])
            if not source:
                raise DomainError("项目还没有原文字幕，无法建立映射")
            mapped_ids = [e["cue_id"] for e in entries]
            if len(set(mapped_ids)) != len(mapped_ids):
                raise DomainError("同一译文字幕不能重复映射")
            if any(cid not in cues for cid in mapped_ids):
                raise DomainError("映射包含不属于该版本的译文字幕", 404)
            missing = sorted(cid for cid in cues if cid not in set(mapped_ids))
            if missing:
                raise DomainError("还有译文字幕缺少映射，请先完成全部映射", 409, extra={"unmapped_cue_ids": missing})
            # 一张译文覆盖连续的一段原文；映射按译文顺序铺满原文，不交叉也不留空
            source_indices = sorted(source)
            expanded: list[int] = []
            for e in sorted(entries, key=lambda x: cues[x["cue_id"]]["cue_index"]):
                if e["source_start_index"] > e["source_end_index"]:
                    raise DomainError("映射区间起点不能大于终点")
                seg = [i for i in source_indices if e["source_start_index"] <= i <= e["source_end_index"]]
                if not seg:
                    raise DomainError("映射区间没有对应任何原文字幕")
                expanded.extend(seg)
            if expanded != source_indices:
                raise DomainError("映射必须按译文顺序连续覆盖全部原文字幕，不能交叉或留空", 409)
            batch_no = current_no + 1
            if current:
                conn.execute("UPDATE mapping_batches SET status='superseded' WHERE id=?", (current["id"],))
            cur = conn.execute(
                "INSERT INTO mapping_batches(version_id,batch_no,source_revision,content_hash,idempotency_key,status,created_by,created_at) VALUES(?,?,?,?,?,'active',?,?)",
                (version_id, batch_no, source_revision, content_hash, idem or None, actor, utcnow()),
            )
            for e in entries:
                conn.execute(
                    "INSERT INTO mappings(batch_id,cue_id,source_start_index,source_end_index,source_revision) VALUES(?,?,?,?,?)",
                    (cur.lastrowid, e["cue_id"], e["source_start_index"], e["source_end_index"], source_revision),
                )
            self._audit(conn, actor, "mapping.saved", "version", version_id, {"batch_id": cur.lastrowid, "batch_no": batch_no, "source_revision": source_revision, "entries": len(entries)})
            batch = conn.execute("SELECT * FROM mapping_batches WHERE id=?", (cur.lastrowid,)).fetchone()
            return self._batch_payload(conn, batch) | {"duplicate": False}

    def get_mappings(self, version_id: int) -> dict[str, Any]:
        """返回最近一个完整映射批次；写入失败时事务回滚，这里永远是可恢复的最新完整状态。"""
        with self.connect() as conn:
            version = self._version(conn, version_id)
            health = self._mapping_health(conn, version)
            entries = [
                {"cue_id": item["id"], "cue_index": item["cue_index"], "text": item["text"], **item["mapping"], "status": item["mapping_status"]}
                for item in health["items"] if item["mapping"]
            ]
            return {"batch": health["batch"], "entries": entries, "complete": health["summary"]["complete"], "summary": health["summary"]}

    def version_detail(self, version_id: int) -> dict[str, Any]:
        with self.connect() as conn:
            version = self._version(conn, version_id)
            health = self._mapping_health(conn, version)
            return {"version": dict(version), "mapping": {"batch": health["batch"], "summary": health["summary"]}, "cues": health["items"]}

    def review_queue(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT v.*,p.owner,p.duration_ms,p.name AS project_name FROM versions v JOIN projects p ON p.id=v.project_id WHERE v.status='review' ORDER BY v.updated_at"
            ).fetchall()
            return [{"version": dict(r), "mapping": self._mapping_health(conn, r)["summary"]} for r in rows]

    def migrate_legacy(self, actor: str = "system") -> dict[str, Any]:
        """旧数据迁移：缺映射的译文字幕保持待确认，确认前不进入复核或交付。"""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = []
            audited = 0
            rows = conn.execute("SELECT v.*,p.owner,p.duration_ms FROM versions v JOIN projects p ON p.id=v.project_id ORDER BY v.id").fetchall()
            for v in rows:
                if not conn.execute("SELECT 1 FROM cues WHERE version_id=? LIMIT 1", (v["id"],)).fetchone():
                    continue
                summary = self._mapping_health(conn, v)["summary"]
                if not summary["pending"] and not summary["stale"] and summary["complete"]:
                    continue
                if not conn.execute("SELECT 1 FROM audit_log WHERE action='migration.pending_mapping' AND entity_type='version' AND entity_id=?", (v["id"],)).fetchone():
                    self._audit(conn, actor, "migration.pending_mapping", "version", v["id"], {"pending": summary["pending"], "stale": summary["stale"]})
                    audited += 1
                bounced = v["status"] in {"review", "approved", "locked"}
                if bounced:
                    conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), v["id"]))
                pending.append({"version_id": v["id"], "language": v["language"], "status": v["status"], "bounced_to_draft": bounced,
                                "pending": summary["pending"], "stale": summary["stale"], "complete": summary["complete"]})
            return {"audited": audited, "pending_versions": pending}

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
            summary = self._mapping_health(conn, version)["summary"]
            if summary["pending"]:
                raise DomainError("存在待确认映射的译文字幕，确认前不能提交复核", 409)
            if summary["stale"]:
                raise DomainError("原文已变更，部分译文映射失效，请重新确认映射", 409)
            if not summary["complete"]:
                raise DomainError("映射尚未连续覆盖全部原文字幕，不能提交复核", 409)
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
            summary = self._mapping_health(conn, version)["summary"]
            if summary["pending"] or summary["stale"] or not summary["complete"]:
                conn.execute("UPDATE versions SET status='draft',updated_at=? WHERE id=?", (utcnow(), version_id))
                self._audit(conn, actor, "version.review_invalidated", "version", version_id, {"reason": "mapping_stale"})
                raise DomainError("原文已变更，复核结果失效，版本已退回草稿", 409)
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
            health = self._mapping_health(conn, version)
            summary = health["summary"]
            if summary["pending"] or summary["stale"] or not summary["complete"]:
                raise DomainError("存在待确认或已失效的映射，不能交付", 409)
            cues = [dict(r) for r in conn.execute("SELECT cue_index,start_ms,end_ms,text FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,))]
            glossary = [dict(r) for r in conn.execute("SELECT source_term,required_translation,forbidden_terms FROM glossaries WHERE project_id=? ORDER BY source_term", (version["project_id"],))]
            mapping = {
                "batch_id": health["batch"]["id"],
                "batch_no": health["batch"]["batch_no"],
                "source_revision": health["batch"]["source_revision"],
                "entries": [{"cue_id": item["id"], "cue_index": item["cue_index"], **item["mapping"]} for item in health["items"]],
            }
            manifest = {"project_id": version["project_id"], "version_id": version_id, "language": version["language"], "version_no": version["version_no"], "cues": cues, "glossary": glossary, "mapping": mapping}
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
            return [dict(r) for r in conn.execute("SELECT * FROM cues WHERE version_id=? ORDER BY cue_index", (version_id,)).fetchall()]

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
    projects = db.list_projects()
    if projects:
        versions = db.list_versions(projects[0]["id"])
        return {"project": int(projects[0]["id"]), "version": int(versions[0]["id"]) if versions else 0}
    project = db.create_project("alice", {"name": "极地纪录片字幕", "source_language": "en", "media_name": "polar.mp4", "media_sha256": "b" * 64, "duration_ms": 120000}, "owner")
    db.set_glossary(project["id"], "alice", {"source_term": "seal", "required_translation": "海豹", "forbidden_terms": ["密封"], "notes": "动物学语境"}, "owner")
    for i, (start, end, text) in enumerate([
        (1000, 3000, "A seal rests on the sea ice."),
        (3500, 5500, "It listens for the tide."),
        (6000, 8000, "Winter is coming."),
    ]):
        db.save_source_cue(project["id"], "alice", {"cue_index": i + 1, "start_ms": start, "end_ms": end, "text": text, "expected_revision": i}, "owner")
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
            if parsed.path == "/api/review-queue":
                return self._send({"queue": self.db.review_queue()})
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
                return self._send(self.db.get_mappings(int(parts[2])))
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "detail":
                return self._send(self.db.version_detail(int(parts[2])))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)} | getattr(exc, "extra", {}), getattr(exc, "status", 400))

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
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "mappings":
                return self._send(self.db.save_mapping_batch(int(parts[2]), actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "versions"] and parts[3] == "comments":
                return self._send(self.db.add_comment(int(parts[2]), actor, body, role), 201)
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
            self._send({"error": str(exc)} | getattr(exc, "extra", {}), getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[subtitle] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="字幕本地化质检与交付服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8009")))
    parser.add_argument("--db", default=os.getenv("SUBTITLE_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库和示例项目")
    parser.add_argument("--migrate", action="store_true", help="迁移旧数据：缺映射的译文字幕标记为待确认")
    args = parser.parse_args()
    db = Database(args.db)
    if args.init:
        seed = seed_demo(db)
        print(f"initialized database at {args.db}; project={seed['project']} version={seed['version']}")
        return
    if args.migrate:
        report = db.migrate_legacy()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"subtitle-qc listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
