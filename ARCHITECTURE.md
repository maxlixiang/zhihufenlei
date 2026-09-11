# 全文只分析一次：程序架构

## 目标

每个未变化的文章内容只发送给 DeepSeek 一次。在分类目录增加、改名、合并或调整边界后，使用本地永久语义档案重新映射，不再次发送原文。

“一次”以 SHA-256 内容哈希为准：路径或文件名变化不等于内容变化；内容哈希变化表示文章成为新版本，需要重新分析。

## 数据分层

```text
articles
  原始文件索引、路径、标题、内容哈希
        │
        │ analyze：只在当前哈希没有成功语义档案时阅读全文
        ▼
article_analyses
  与目录无关的永久语义档案
        │
        │ 初始映射或 remap：只发送语义档案
        ▼
category_assignments
  某个 taxonomy_version 下的目录映射
        │
        └─ api_usage：按阶段记录每次调用的 token
```

旧表 `classifications` 和 `classification_runs` 仅保留首批试跑审计历史，不再作为新流程的数据来源。

## `article_analyses`

主要字段：

- `content_hash`：内容版本身份
- `analysis_version`、`prompt_version`、模型与提供方
- `summary_short`、`summary_detailed`
- `primary_purpose`
- `main_topics`、`tags`、`key_points`、`entities`
- `content_type`、`scope`、`actionability`、`time_sensitivity`
- `candidate_new_topics`
- 原始结构化响应和错误记录

语义档案尽量保存足以支持未来重新分类的信息，但不保存或复制文章全文。

## `category_assignments`

主要字段：

- 关联的 `analysis_id`
- `taxonomy_version`、映射提示词版本、模型与来源
- 唯一一级、二级目录
- 最多两个备选目录
- 分类理由、置信度、审核状态
- 候选新主题和错误记录

一篇文章可以因分类标准升级而拥有多条历史映射，但每个时点只使用最新映射。语义档案不随目录变化而重建。

## `api_usage`

每次成功 API 调用记录：

- 阶段：全文分析、语义重映射或旧流程
- 输入 token
- 缓存命中与未命中 token
- 输出 token
- 总 token

`status` 命令显示累计用量，便于依据真实样本估算剩余文章成本。

## 命令边界

### `scan`

只读扫描原始库，计算哈希并更新 `articles`。不调用 API。

### `analyze`

只选择当前内容哈希没有成功语义档案的文章。一次 API 请求同时生成永久语义档案和初始目录映射。

默认完整分析上限为30万字符；当前最长文章约15.2万字符，因此现有2330篇均可完整发送。超过上限时程序报错并停止处理该篇，不会静默截掉正文中段。

调用模型前会移除纯图片路径、SVG 和 data URI 等无语义噪声，但保留所有可读文本；正文与评论区分段，评论仅作为次要语境。预处理不使用字符截断。

### `remap`

只选择当前分类版本没有成功映射的语义档案。发送摘要、主题、标签、关键观点等紧凑数据，不读取原始 Markdown。

### `export`

组合每篇文章的当前语义档案和最新目录映射，输出可迁移 JSONL 或待审核清单。

### `copy-pilot`

只复制一个明确的成功分析批次，且批次不得超过10篇。Markdown 与同名图片目录作为一个文章包写入同一个分类目录，以保持 Obsidian 相对图片引用有效。分类副本生成 YAML Front Matter，正文保持完整；数据库分别记录原始内容哈希与生成副本哈希。复制前校验原文仍位于只读源库且内容哈希未变化，复制后逐个校验正文和附件哈希；拒绝覆盖同名不同内容。待审核结果进入 `_待审核`，每次操作写入 `copy_runs`、`copy_items`、`copy_outputs`、`copy_attachments` 和项目内 JSON 清单。

程序没有移动、重命名或删除文章的命令；全量复制仍未开放。

## 现有数据迁移

首批10篇已在旧流程中完成全文分析。为遵守“全文只分析一次”，系统不重读它们，而是把原有摘要、标签、用途和分类理由迁移为 `legacy-1` 语义档案。它们缺少新版档案的部分丰富字段，但足以用于当前目录重映射。

后续2320篇使用 `analysis_version=2` 的完整语义档案格式。
