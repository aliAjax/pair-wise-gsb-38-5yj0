import json
import tempfile
import unittest
from pathlib import Path

from app import Database, DomainError, seed_demo


class BaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")
        seed = seed_demo(self.db)
        self.project, self.version = seed["project"], seed["version"]
        self.db.assign(self.version, "alice", {"user": "bob", "role": "translator"}, "owner")
        self.db.assign(self.version, "alice", {"user": "carol", "role": "reviewer"}, "owner")

    def tearDown(self):
        self.tmp.cleanup()

    def source_cue_id(self, cue_index):
        return next(c["id"] for c in self.db.list_source_cues(self.project) if c["cue_index"] == cue_index)

    def version_row(self):
        return next(v for v in self.db.list_versions() if v["id"] == self.version)


class SubtitleQCFlowTest(BaseTestCase):
    def test_full_review_lock_delivery_and_overwrite_protection(self):
        cue = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "seal 海豹在冰面", "expected_revision": 0})
        self.assertEqual(cue["version_revision"], 1)
        # 一张译文覆盖连续的一段原文，映射批次记录所依据的原文修订
        batch = self.db.save_mapping_batch(self.version, "bob", {"base_batch_no": 0, "mappings": [{"cue_id": cue["id"], "source_start_index": 1, "source_end_index": 3}]})
        self.assertEqual(batch["batch"]["batch_no"], 1)
        self.assertEqual(batch["entries"][0]["source_revision"], 3)
        comment = self.db.add_comment(self.version, "carol", {"cue_id": cue["id"], "time_ms": 1200, "body": "术语正确，请确认冻结时间"}, "reviewer")
        self.assertEqual(comment["time_ms"], 1200)
        self.db.submit(self.version, "bob")
        approved = self.db.review(self.version, "carol", {"decision": "approve", "comment": "通过"}, "reviewer")
        self.assertEqual(approved["status"], "approved")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        self.assertEqual(len(delivery["snapshot_hash"]), 64)
        # 交付快照携带映射明细
        manifest = json.loads(delivery["manifest"])
        self.assertEqual(manifest["mapping"]["batch_no"], 1)
        self.assertEqual(manifest["mapping"]["entries"][0]["source_start_index"], 1)
        self.assertEqual(manifest["mapping"]["entries"][0]["source_end_index"], 3)
        with self.assertRaisesRegex(DomainError, "只有草稿"):
            self.db.save_cue(self.version, "bob", {"cue_id": cue["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 1})

    def test_revision_overlap_glossary_and_permissions(self):
        first = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "其他成员修改"):
            self.db.save_cue(self.version, "bob", {"cue_id": first["id"], "cue_index": 1, "start_ms": 1000, "end_ms": 2500, "text": "海豹", "expected_revision": 0})
        with self.assertRaisesRegex(DomainError, "重叠"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 2500, "end_ms": 4000, "text": "另一句", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "禁用译法"):
            self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "密封装置", "expected_revision": 1})
        with self.assertRaisesRegex(DomainError, "权限"):
            self.db.save_cue(self.version, "carol", {"cue_index": 2, "start_ms": 3500, "end_ms": 4000, "text": "海豹", "expected_revision": 1})


class MappingBatchTest(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.db.assign(self.version, "alice", {"user": "dan", "role": "translator"}, "owner")
        self.c1 = self.db.save_cue(self.version, "bob", {"cue_index": 1, "start_ms": 1000, "end_ms": 3000, "text": "海豹在冰面休息", "expected_revision": 0})
        self.c2 = self.db.save_cue(self.version, "bob", {"cue_index": 2, "start_ms": 3500, "end_ms": 5500, "text": "它倾听潮汐", "expected_revision": 1})

    def good_mappings(self):
        return [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 2},
            {"cue_id": self.c2["id"], "source_start_index": 3, "source_end_index": 3},
        ]

    def map_batch(self, mappings, base=0, actor="bob", **kw):
        return self.db.save_mapping_batch(self.version, actor, {"base_batch_no": base, "mappings": mappings, **kw})

    def test_mapping_must_tile_without_gaps_or_crossing(self):
        # 留空：原文 2 没人覆盖
        with self.assertRaisesRegex(DomainError, "交叉或留空"):
            self.map_batch([
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
                {"cue_id": self.c2["id"], "source_start_index": 3, "source_end_index": 3},
            ])
        # 交叉：译文顺序与原文区间不一致
        with self.assertRaisesRegex(DomainError, "交叉或留空"):
            self.map_batch([
                {"cue_id": self.c1["id"], "source_start_index": 3, "source_end_index": 3},
                {"cue_id": self.c2["id"], "source_start_index": 1, "source_end_index": 2},
            ])
        # 两张译文覆盖同一段原文
        with self.assertRaisesRegex(DomainError, "交叉或留空"):
            self.map_batch([
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 3},
                {"cue_id": self.c2["id"], "source_start_index": 3, "source_end_index": 3},
            ])
        # 有译文缺映射
        with self.assertRaisesRegex(DomainError, "缺少映射"):
            self.map_batch([{"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 3}])
        # 区间没有对应任何原文
        with self.assertRaisesRegex(DomainError, "没有对应任何原文"):
            self.map_batch([
                {"cue_id": self.c1["id"], "source_start_index": 9, "source_end_index": 10},
                {"cue_id": self.c2["id"], "source_start_index": 1, "source_end_index": 3},
            ])
        # 合法：一张译文覆盖连续的一段原文，批次铺满全部原文
        batch = self.map_batch(self.good_mappings())
        self.assertFalse(batch["duplicate"])
        self.assertEqual(batch["batch"]["batch_no"], 1)
        self.assertEqual(batch["batch"]["source_revision"], 3)
        entries = {e["cue_id"]: e for e in batch["entries"]}
        self.assertEqual(entries[self.c1["id"]]["source_revision"], 3)
        detail = self.db.version_detail(self.version)
        self.assertTrue(detail["mapping"]["summary"]["complete"])
        self.assertEqual([c["mapping_status"] for c in detail["cues"]], ["fresh", "fresh"])

    def test_source_change_invalidates_affected_mappings_and_review(self):
        self.map_batch(self.good_mappings())
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        # 原文 2 变更（被 c1 覆盖）：复核结果失效，版本退回草稿
        self.db.save_source_cue(self.project, "alice", {"cue_id": self.source_cue_id(2), "cue_index": 2, "start_ms": 3500, "end_ms": 5500, "text": "It listens for the tide, carefully.", "expected_revision": 3}, "owner")
        self.assertEqual(self.version_row()["status"], "draft")
        detail = self.db.version_detail(self.version)
        status = {c["id"]: c["mapping_status"] for c in detail["cues"]}
        self.assertEqual(status[self.c1["id"]], "stale")  # 受影响译文失效
        self.assertEqual(status[self.c2["id"]], "fresh")  # 其他句子照旧
        # 重新确认映射后恢复提交；复核期间原文再变，复核队列同步剔除新鲜
        self.map_batch(self.good_mappings(), base=1)
        self.db.submit(self.version, "bob")
        queue = self.db.review_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["mapping"]["fresh"], 2)
        self.assertTrue(queue[0]["mapping"]["complete"])
        self.db.save_source_cue(self.project, "alice", {"cue_id": self.source_cue_id(3), "cue_index": 3, "start_ms": 6000, "end_ms": 8000, "text": "Winter is coming soon.", "expected_revision": 4}, "owner")
        self.assertEqual(self.db.review_queue(), [])
        with self.assertRaisesRegex(DomainError, "复核阶段"):
            self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")

    def test_concurrent_batch_first_wins_and_duplicates_are_deduped(self):
        first = self.map_batch(self.good_mappings(), idempotency_key="k-1")
        rival = [
            {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
            {"cue_id": self.c2["id"], "source_start_index": 2, "source_end_index": 3},
        ]
        # 两人基于同一批次同时提交：先到的生效，后到的保留输入并看到冲突
        with self.assertRaises(DomainError) as ctx:
            self.map_batch(rival, actor="dan")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.extra["your_input"]["mappings"], rival)
        self.assertEqual(ctx.exception.extra["current_batch"]["batch"]["id"], first["batch"]["id"])
        # 重复提交不重复入库：相同内容或相同幂等键都返回已入库批次
        dup = self.map_batch(self.good_mappings())
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["batch"]["id"], first["batch"]["id"])
        retry = self.map_batch(self.good_mappings(), idempotency_key="k-1")
        self.assertEqual(retry["batch"]["id"], first["batch"]["id"])
        with self.db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM mapping_batches WHERE version_id=?", (self.version,)).fetchone()["c"]
        self.assertEqual(n, 1)
        # 后到的人基于最新批次重新提交后生效
        second = self.map_batch(rival, base=1, actor="dan")
        self.assertEqual(second["batch"]["batch_no"], 2)
        self.assertEqual(self.db.get_mappings(self.version)["batch"]["batch_no"], 2)

    def test_failed_write_recovers_latest_complete_batch(self):
        first = self.map_batch(self.good_mappings())
        with self.assertRaisesRegex(DomainError, "交叉或留空"):
            self.map_batch([
                {"cue_id": self.c1["id"], "source_start_index": 1, "source_end_index": 1},
                {"cue_id": self.c2["id"], "source_start_index": 3, "source_end_index": 3},
            ], base=1)
        got = self.db.get_mappings(self.version)
        self.assertEqual(got["batch"]["id"], first["batch"]["id"])
        self.assertTrue(got["complete"])
        self.assertEqual([e["status"] for e in got["entries"]], ["fresh", "fresh"])

    def test_legacy_cues_stay_pending_until_confirmed(self):
        # 旧数据：有译文字幕但没有映射，全部待确认
        detail = self.db.version_detail(self.version)
        self.assertEqual([c["mapping_status"] for c in detail["cues"]], ["pending", "pending"])
        with self.assertRaisesRegex(DomainError, "待确认"):
            self.db.submit(self.version, "bob")
        # 模拟旧系统中已在复核的版本，迁移时退回草稿
        with self.db.connect() as conn:
            conn.execute("UPDATE versions SET status='review' WHERE id=?", (self.version,))
        report = self.db.migrate_legacy()
        self.assertEqual(report["audited"], 1)
        self.assertEqual(report["pending_versions"][0]["version_id"], self.version)
        self.assertTrue(report["pending_versions"][0]["bounced_to_draft"])
        self.assertEqual(self.version_row()["status"], "draft")
        # 迁移幂等：重复执行不重复记录
        self.assertEqual(self.db.migrate_legacy()["audited"], 0)
        # 确认映射后恢复流程
        self.map_batch(self.good_mappings())
        self.db.submit(self.version, "bob")
        self.assertEqual(len(self.db.review_queue()), 1)

    def test_delivery_snapshot_records_mapping(self):
        self.map_batch(self.good_mappings())
        self.db.submit(self.version, "bob")
        self.db.review(self.version, "carol", {"decision": "approve"}, "reviewer")
        self.db.lock(self.version, "alice")
        delivery = self.db.deliver(self.version, "alice")
        mapping = json.loads(delivery["manifest"])["mapping"]
        self.assertEqual(mapping["batch_no"], 1)
        self.assertEqual(mapping["source_revision"], 3)
        self.assertEqual(len(mapping["entries"]), 2)
        self.assertEqual(mapping["entries"][0]["source_start_index"], 1)
        self.assertEqual(mapping["entries"][0]["source_end_index"], 2)
        self.assertTrue(mapping["entries"][0]["source_text"])


if __name__ == "__main__":
    unittest.main()
