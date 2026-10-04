import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class MappingFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")
        self.c1 = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "一只海豹在冰面休息。", "expected_revision": 0})
        self.c2 = self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3000, "end_ms": 5000, "text": "黄昏时风更冷了。", "expected_revision": 1})

    def tearDown(self):
        self.tmp.cleanup()

    def _source_ids(self):
        rows = self.db.list_source_cues(self.project)
        return [r["id"] for r in rows]

    def _map(self, key=None, expected=0):
        payload = {
            "expected_mapping_revision": expected,
            "mappings": [
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
                {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 2},
            ],
        }
        if key:
            payload["idempotency_key"] = key
        return self.db.submit_mappings(self.version, "bob", payload)

    # ------------------------------------------------------------------

    def test_full_flow_with_mapping_in_delivery_snapshot(self):
        result = self._map(key="batch-1")
        self.assertEqual(result["mapping_revision"], 1)
        self.assertTrue(result["alignment"]["complete"])
        cues = self.db.list_cues(self.version)
        self.assertEqual({c["mapping_state"] for c in cues}, {"confirmed"})
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        manifest = json.loads(delivery["manifest"])
        self.assertEqual(manifest["mapping"]["batch_id"], result["batch_id"])
        self.assertEqual(len(manifest["source_cues"]), 2)
        self.assertEqual(len(manifest["mapping"]["mappings"]), 2)

    def test_mapping_must_be_contiguous_no_gap_no_overlap_full_cover(self):
        with self.assertRaisesRegex(DomainError, "全部译文字幕"):
            self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 0, "mappings": [
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 2},
            ]})
        good = [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 2},
            {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 2},
        ]
        with self.assertRaisesRegex(DomainError, "交叉或留空"):
            self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 0, "mappings": good})
        self.db.save_source_cue(self.project, "alice", {"cue_index": 3, "start_ms": 5000, "end_ms": 7000, "text": "Night falls quietly."}, "owner")
        with self.assertRaisesRegex(DomainError, "末尾留空"):
            self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 0, "mappings": [
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
                {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 2},
            ]})

    def test_mapping_records_source_revision_basis_and_merges_segments(self):
        # 三条原文、两条译文：第一张译文字幕覆盖连续的两条原文字幕。
        self.db.save_source_cue(self.project, "alice", {"cue_index": 3, "start_ms": 5000, "end_ms": 7000, "text": "Night falls quietly."}, "owner")
        result = self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 0, "mappings": [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 2},
            {"cue_id": self.c2["id"], "source_start_index": 3, "source_end_index": 3},
        ]})
        self.assertTrue(result["alignment"]["complete"])
        first = result["alignment"]["mappings"][0]
        self.assertEqual((first["source_start_index"], first["source_end_index"]), (1, 2))
        self.assertEqual(len(first["source_cue_ids"]), 2)

    def test_source_change_invalidates_only_affected_mapping_and_review(self):
        self._map(key="batch-1")
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        # 只改原文第 1 条：c1 失效，c2 照旧；版本退回草稿。
        self.db.save_source_cue(self.project, "alice", {"cue_id": self._source_ids()[0], "cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "A young seal rests on the ice."}, "owner")
        alignment = self.db.list_mappings(self.version)
        states = {m["cue_id"]: m["state"] for m in alignment["mappings"]}
        self.assertEqual(states[self.c1["id"]], "stale")
        self.assertEqual(states[self.c2["id"]], "confirmed")
        version = self.db.list_versions(self.project)[0]
        self.assertEqual(version["status"], "draft")
        with self.assertRaisesRegex(DomainError, "原文已修订"):
            self.db.submit(self.version, "bob")
        # 重新提交映射后恢复完整。
        fixed = self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 1, "mappings": [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
            {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 2},
        ]})
        self.assertTrue(fixed["alignment"]["complete"])

    def test_timestamp_only_change_does_not_invalidate(self):
        self._map(key="batch-1")
        self.db.save_source_cue(self.project, "alice", {"cue_id": self._source_ids()[0], "cue_index": 1, "start_ms": 900, "end_ms": 3000, "text": "A seal rests on the ice."}, "owner")
        self.assertTrue(self.db.list_mappings(self.version)["complete"])

    def test_concurrent_batch_first_writer_wins_second_sees_conflict_and_keeps_input(self):
        first = self._map(key="batch-1")
        self.assertEqual(first["mapping_revision"], 1)
        try:
            self._map(key="batch-2", expected=0)
        except DomainError as exc:
            self.assertEqual(exc.status, 409)
            self.assertTrue(exc.details["conflict"])
            self.assertEqual(exc.details["current_mapping_revision"], 1)
            # 后到的人保留输入并看到赢家版本。
            self.assertEqual(len(exc.details["your_input"]), 2)
            self.assertEqual(exc.details["current_alignment"]["batch_id"], first["batch_id"])
        else:
            self.fail("并发批次应报 409 冲突")

    def test_idempotent_repeat_submit_does_not_duplicate(self):
        first = self._map(key="same-key")
        second = self._map(key="same-key", expected=0)
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["batch_id"], first["batch_id"])
        with self.db.connect() as conn:
            count = conn.execute("SELECT COUNT(*) c FROM mapping_batches WHERE version_id=?", (self.version,)).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_recover_from_pending_batch_restores_last_complete(self):
        complete = self._map(key="batch-1")
        # 模拟写入失败：手工插入一个未完成批次。
        with self.db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO mapping_batches(version_id,status,base_mapping_revision,idempotency_key,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (self.version, "pending", 1, None, "bob", "2026-01-01T00:00:00+00:00"),
            )
            pending_id = cur.lastrowid
            conn.execute(
                "INSERT INTO cue_mappings(batch_id,version_id,cue_id,ordinal,source_start_index,source_end_index,source_revisions) VALUES(?,?,?,?,?,?,?)",
                (pending_id, self.version, self.c1["id"], 0, 1, 1, "[[1,1]]"),
            )
        recovered = self.db.recover_mappings(self.version, "bob")
        self.assertEqual(recovered["removed_pending_batches"], [pending_id])
        self.assertEqual(recovered["restored_batch_id"], complete["batch_id"])
        with self.db.connect() as conn:
            pending_left = conn.execute("SELECT COUNT(*) c FROM mapping_batches WHERE status='pending'").fetchone()["c"]
        self.assertEqual(pending_left, 0)

    def test_legacy_unmapped_cues_are_pending_and_blocked_from_review_and_delivery(self):
        # 迁移语义：有译文但没有任何批次 → 全部待确认，复核队列不放行。
        queue = self.db.review_queue(self.version)
        self.assertFalse(queue["ready_for_review"])
        self.assertEqual({c["id"] for c in queue["pending_cues"]}, {self.c1["id"], self.c2["id"]})
        with self.assertRaisesRegex(DomainError, "必须先建立原文"):
            self.db.submit(self.version, "bob")
        # 补齐映射后进入复核。
        self._map(key="batch-1")
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "ok"}, "reviewer")
        self.assertEqual(approved["status"], "approved")

    def test_new_source_cue_leaves_gap_until_remapped(self):
        self._map(key="batch-1")
        self.db.save_source_cue(self.project, "alice", {"cue_index": 3, "start_ms": 5000, "end_ms": 7000, "text": "Night falls quietly."}, "owner")
        alignment = self.db.list_mappings(self.version)
        self.assertFalse(alignment["complete"])
        self.assertIn([3, 3], alignment["uncovered_source_ranges"])
        with self.assertRaisesRegex(DomainError, "留空"):
            self.db.submit(self.version, "bob")
        c3 = self.db.save_cue(self.version, "bob", {"cue_index": 3, "start_ms": 5000, "end_ms": 7000, "text": "夜幕静静降临。", "expected_revision": 2})
        self.db.submit_mappings(self.version, "bob", {"expected_mapping_revision": 1, "mappings": [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
            {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 2},
            {"cue_id": c3["id"], "source_start_index": 3, "source_end_index": 3},
        ]})
        self.assertTrue(self.db.list_mappings(self.version)["complete"])

    def test_only_owner_can_edit_source_cues(self):
        with self.assertRaisesRegex(DomainError, "原文字幕"):
            self.db.save_source_cue(self.project, "bob", {"cue_index": 3, "start_ms": 5000, "end_ms": 7000, "text": "x"}, "translator")


class LegacyDatabaseMigrationTest(unittest.TestCase):
    def test_old_db_gets_default_pending_state_and_blocks_delivery(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        import sqlite3

        path = str(Path(tmp.name) / "legacy.db")
        # 用迁移前的旧结构造一个库：无 source_cues / mapping 表，cues 无 mapping_state。
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE projects(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT UNIQUE,source_language TEXT,duration_ms INTEGER,owner TEXT,media_name TEXT,media_sha256 TEXT,created_at TEXT);
            CREATE TABLE versions(id INTEGER PRIMARY KEY AUTOINCREMENT,project_id INTEGER,language TEXT,version_no INTEGER,parent_id INTEGER,status TEXT DEFAULT 'draft',revision INTEGER DEFAULT 0,created_by TEXT,created_at TEXT,updated_at TEXT);
            CREATE TABLE cues(id INTEGER PRIMARY KEY AUTOINCREMENT,version_id INTEGER,cue_index INTEGER,start_ms INTEGER,end_ms INTEGER,text TEXT,updated_by TEXT,updated_at TEXT);
            INSERT INTO projects VALUES(1,'旧项目','en',120000,'alice','m.mp4','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','2026-01-01T00:00:00+00:00');
            INSERT INTO versions VALUES(1,1,'zh-CN',1,NULL,'approved',0,'bob','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            INSERT INTO cues VALUES(1,1,1,1000,2000,'旧译文','bob','2026-01-01T00:00:00+00:00');
            """
        )
        conn.commit()
        conn.close()
        db = Database(path)
        cues = db.list_cues(1)
        self.assertEqual(cues[0]["mapping_state"], "pending")
        with self.assertRaisesRegex(DomainError, "必须先建立原文"):
            db.deliver(1, "alice")


if __name__ == "__main__":
    unittest.main()
