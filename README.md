# 影视字幕本地化质检

一个仅使用 Python 标准库实现的字幕翻译、时间轴审核和交付服务。SQLite 保存项目、原文字幕、字幕版本、原文↔译文映射批次、人员分配、时间点评论、术语表、复核意见和交付快照。

## 运行

```bash
python app.py --init
python app.py --port 8009
```

打开 <http://127.0.0.1:8009>。`--init` 会创建示例纪录片项目、两条英文原文字幕、`zh-CN` 草稿版本和一条术语规则。数据库默认是 `subtitle_qc.db`，可用 `--db` 或 `SUBTITLE_DB` 修改。

## 流程

1. 负责人创建项目、字幕版本、术语规则，并维护**原文字幕**（`source_cues`，每条带自己的 `revision`）。
2. 为版本分配 `translator`、`timeline`、`reviewer`。
3. 翻译或时间轴成员保存译文字幕；每项包含 `expected_revision`，旧页面提交会返回 409。
4. 提交**映射批次**：一张译文字幕覆盖连续的一段原文字幕，映射之间不交叉、不留空，且双向全覆盖；每条映射记下所依据的各原文条修订号（`source_revisions`）。
5. 成员可对具体字幕或毫秒时间点添加评论。
6. 翻译/时间轴成员提交复核；只有映射完整（无 `pending`、无 `stale`、无未覆盖原文）才能提交，分配的非创建人复核人批准或退回。
7. 负责人锁定已批准版本，再执行交付；交付快照内嵌当前映射批次、原文字幕修订和译文。

### 统一对应关系的语义

原文字幕、译文字幕、映射批次和交付快照共用同一份对应关系（以最近的 `complete` 批次为准）：

- **不交叉、不留空**：批次内映射按原文序号逐条相接，覆盖项目全部原文字幕；每条译文字幕恰好出现一次。
- **依据原文修订**：每条映射保存其覆盖的原文条及各自修订号。原文**文本**变化会抬高该条修订号（仅调时间轴不抬高），覆盖它的映射派生为 `stale`，对应版本若在复核中/已批准则退回草稿；其他句子的映射照旧 `confirmed`。
- **并发提交**：提交时带 `expected_mapping_revision`。两个人同时提交同一批映射时，先到的生效（版本号 +1），后到的收到 409，响应里包含 `your_input`（原样回传）和 `current_alignment`（赢家版本）。
- **写入失败恢复**：批次先以 `pending` 落库、明细写完后再置 `complete`。`POST /api/versions/{id}/recover-mappings` 会删除残留的不完整批次，并从最近完整批次恢复对应关系。
- **重复提交幂等**：同一 `idempotency_key` 的重复提交不重复入库，直接返回首个批次（`idempotent_replay: true`）。
- **旧数据迁移**：旧库自动补列，缺映射的译文字幕标记为 `pending`（待确认），确认前不进入复核队列、不能提交/批准/交付。
- 详情（译文字幕的 `mapping_state`）、`GET .../mappings`、复核队列 `GET .../review-queue` 和交付快照全部读取同一份映射派生结果。

字幕保存会验证时长范围、起点小于终点、字幕重叠、序号冲突和术语表。术语表中配置的禁用译法会直接阻止保存；指定译法可用。

## API

所有身份通过 `X-User`、`X-Role` 请求头模拟，角色包括 `owner`、`admin`、`translator`、`reviewer`、`timeline`。

- `POST /api/projects`：创建项目和成片校验信息。
- `POST /api/projects/{id}/versions`：创建目标语言版本，可指定同语言父版本。
- `POST /api/projects/{id}/glossary`：设置指定译法和禁用词。
- `GET|POST /api/projects/{id}/source-cues`：查看/维护原文字幕（仅负责人）；文本修订会使相关映射失效。
- `POST /api/versions/{id}/assignments`：分配角色。
- `POST /api/versions/{id}/cues`：新增或修改译文字幕，要求 `expected_revision`；新条目映射状态为待确认。
- `GET /api/versions/{id}/mappings`：查看当前对应关系（`complete`、各条 `confirmed/stale`、`pending_cue_ids`、`uncovered_source_ranges`）。
- `POST /api/versions/{id}/mappings`：提交完整映射批次 `{expected_mapping_revision, idempotency_key?, mappings:[{cue_id,source_start_index,source_end_index}]}`。
- `POST /api/versions/{id}/recover-mappings`：清除残留 `pending` 批次并恢复最近完整批次。
- `GET /api/versions/{id}/review-queue`：复核队列，标明可复核、失效和待确认条目。
- `POST /api/versions/{id}/comments`：按具体时间毫秒或字幕 ID 评论。
- `POST /api/versions/{id}/submit|review|lock|deliver`：完成审核交付状态机；映射不完整时前三步及交付均返回 409 并附带 `alignment`。
- `GET /api/versions/{id}/cues|comments`、`GET /api/deliveries`：查看结果。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：带映射的完整复核交付流程与快照内容、连续性/交叉/留空/全覆盖校验、合并原文段、原文修订的精准失效（仅受影响映射、复核退回、重新确认恢复）、只调时间轴不失效、并发先到先得与冲突回传、幂等不重复入库、`pending` 批次恢复、旧库迁移待确认拦截、新增原文留空、原文字幕权限。
