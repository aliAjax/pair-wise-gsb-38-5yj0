# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、原文字幕、字幕版本、人员分配、时间点评论、术语表、原文-译文映射、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、三句英文原文字幕、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

旧库升级时表结构自动补齐；再执行 `python app.py --migrate` 做一次数据迁移：缺映射的译文字幕被标记为待确认，确认前不进入复核或交付，迁移可重复执行且幂等。

## 流程

1. 负责人创建项目、字幕版本和术语规则，并维护原文字幕；每次保存原文都会推进项目的 `source_revision`。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存译文字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 翻译/时间轴成员提交映射批次：一张译文字幕覆盖连续的一段原文字幕，映射按译文顺序铺满全部原文，不交叉也不留空；每条映射记下所依据的 `source_revision`。
5. 成员可对具体字幕或毫秒时间点添加评论。
6. 翻译/时间轴成员提交复核（要求全部译文映射已确认且未失效），分配的非创建人复核人批准或退回。
7. 负责人锁定已批准版本，再执行交付。
8. 交付时生成确定性的 SHA-256 快照，快照清单包含映射批次号、原文修订和逐条映射明细；同语言的新交付会把旧版本标记为 `superseded`，但旧快照不会删除或覆盖。

原文字幕一变，覆盖该段原文的译文映射失效（`stale`），其余句子照旧（`fresh`）；处于复核、已批准或已锁定状态的版本会退回草稿，复核结果失效。重新提交映射批次确认后即可恢复流程。

映射批次的并发与恢复语义：

- 两人同时基于同一批次提交时，先到的版本生效；后到的人收到 409，响应里带回最新批次和自己提交的输入，重新基于最新批次提交即可。
- 重复提交不重复入库：相同 `idempotency_key` 或相同内容的重试直接返回已入库批次；但原文变更后，相同区间的重新提交视为有意义的再确认，会按新的原文修订入库。
- 批次写入是单事务，写入失败后从 `GET /api/versions/{id}/mappings` 拿到的永远是最近一个完整批次。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `POST /api/projects/{id}/source-cues`：新增或修改原文字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改译文字幕，要求 `expected_revision`。
- `POST /api/versions/{id}/mappings`：提交映射批次，携带 `base_batch_no`、可选 `idempotency_key` 和 `mappings`（`cue_id` + `source_start_index`/`source_end_index`）。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机。
- `GET /api/projects/{id}/source-cues`、`GET /api/versions/{id}/cues|comments|mappings|detail`、`GET /api/review-queue`、`GET /api/deliveries`：查看结果；详情、复核队列和交付快照都基于同一份映射。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整复核交付流程、锁定覆盖保护、旧修订冲突、时间轴重叠、术语禁用、人员权限、映射平铺校验（不交叉不留空）、原文变更失效、并发批次先到生效、幂等去重、失败恢复和旧数据迁移待确认。
