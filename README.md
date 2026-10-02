# 知乎文章智能分类项目

本项目用于把按时间保存的知乎点赞、收藏文章，逐步整理为适合长期阅读、检索和积累的个人知识分类库。

当前已进入分类文章库的 Obsidian 试用阶段。规则1.1下的2330篇文章已按用户明确授权完成首次全量复制；原始库仍永久只读，后续重建或增量写入仍须得到明确授权。

## 三个目录的职责

- 原始文章库（只读）：`F:\save_zhihu_activity\save_zhihu_activity\原始数据备份-不要删除-9月份爬取`
- 本项目：`F:\知乎\知乎文章智能分类项目`
- 分类文章库：`F:\知乎\知乎分类文章库`

原始文章库是事实来源，必须始终保持不动。未来获得明确批准后，程序只能把文章副本写入分类文章库，不能用分类库替代或反向修改原始库。

## 项目目标

这不是一次性整理现有约两千篇文章，而是一套可以持续接收新文章、发现新主题、修订分类标准并保留历史记录的长期系统。

最终目标包括：

1. 建立稳定但可演进的一级、二级分类体系。
2. 让每篇文章根据“对用户的主要价值和用途”获得唯一分类。
3. 保存分类依据、置信度、规则版本及审核状态，便于复查。
4. 发现现有体系无法覆盖的新主题，但只有用户批准后才能写入正式分类标准。
5. 在不改变原始文章的前提下，按批准结果生成可重新构建的分类文章库。

## 当前共识

- 分类只保留两级：一级目录和二级目录。
- 每篇文章只归入一个二级目录，不在多个文件夹重复保存。
- 分类优先依据文章对用户的实际价值和用途，而非表面关键词。
- 五个核心一级类别依次为：健康、商业挣钱、认识世界、思维模型与思想方法、个人成长。
- 商业挣钱与投资理财必须分开；前者解决“如何获得收入和本金”，后者解决“如何让已有资金增值”。
- 人工智能和两性与亲密关系是独立的普通一级类别，不属于五个核心类别。
- 当前已确认 19 个一级类别：5 个核心类别和 14 个普通类别。
- 14 个普通类别为：投资理财、人工智能、计算机与数字技术、两性与亲密关系、法律与法律实务、教育与考试、历史与文明、军事与战争、自然科学、文化娱乐、体育竞技、生活实用与消费、内容创作与传播、宗教、命理与传统术数。
- 当前文章很少但概念明确、边界清楚且可能长期积累的主题，可以保留为独立分类。
- 已正式增加“幽默段子与搞笑内容”“美女与视觉欣赏”“明末清初与清史争议”“规则利用与现实策略”四个二级目录。
- 分类规则 `1.1` 已正式增加“健康／个人安全防护”。
- `_待审核/图片为主待识别` 是已确认的特殊审核区，不是正式主题分类；图片被识别后必须改入真实主题目录。

详细规则见 [CLASSIFICATION_RULES.md](CLASSIFICATION_RULES.md)，机器可读草案见 [taxonomy.yaml](taxonomy.yaml)，已确认决策与变更历史见 [DECISIONS.md](DECISIONS.md)，全文只分析一次的技术设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，模型提示词和未知分类处理见 [DEEPSEEK_WORKFLOW.md](DEEPSEEK_WORKFLOW.md)。

## 未来系统的职责分工

- DeepSeek 或其他大模型：批量阅读文章，提出分类、理由、置信度和候选新主题。
- Codex：维护规则、开发和检查程序、抽查分类结果、整理疑难项，并把用户决定写入正式文件。
- 本地程序：发现新增文章、调用模型、校验结构化结果、记录状态、生成审核清单，并在获得明确批准后复制文章。
- 用户：批准分类标准变化、新增分类、疑难项处理方式，以及正式复制批次。

模型提供方应当可替换，程序不能把规则和数据流程绑定到 DeepSeek。

## 推荐的未来处理流程

```text
扫描原始库（只读）
  → 识别新增或变更文章
  → 读取当前分类规则
  → 模型给出唯一分类、理由和置信度
  → 程序校验结果
  → 高置信度结果进入分类清单
  → 低置信度／新主题／异常进入审核报告
  → 用户批准规则或批次
  → 只重新处理受影响文章
  → 获准后复制到分类文章库
```

当前已实现扫描、永久语义档案、分类映射、人工审核、质量报告和受授权保护的分类库复制。

## 日常新增文章：推荐入口

将新 Markdown 及同名附件目录手动放入原始文章库，保留已有文件和项目数据库。然后在项目目录执行：

```powershell
cd F:\知乎\知乎文章智能分类项目
python -m zhihu_classifier update --confirm-copy
```

本次命令表示授权将新文章内容发送给配置的 DeepSeek，并将分类副本追加到分类文章库。它会扫描整库、复用相同内容的档案、分析整批待处理文章，再追加复制，不受旧默认10篇限制。失败文章每次尝试一次，成功文章仍可复制；结束时输出新增、复用、跳过、待处理、冲突和失败数量。退出码0表示本次队列没有遗留错误或冲突，2表示需要查看汇总及复制清单。外部模型调用受网络和服务状态影响。

如需先查看分类再复制，分开执行：

```powershell
python -m zhihu_classifier scan
python -m zhihu_classifier analyze --limit 50
python -m zhihu_classifier export
python -m zhihu_classifier copy-new --confirm-copy
```

`--limit 50` 只限制本次分析数量；已有失败文章也可能占用名额。`copy-new` 不调用模型，可复制任意数量的已分类待输出文章；不能处理的文章继续留在数据库中。两条新命令必须显式添加 `--confirm-copy`，本次开发不代表对真实分类库的任何复制授权。

- 已成功输出的文章按历史源哈希跳过，兼容首次2330篇全量复制记录和旧试运行记录；保护 `.obsidian`、手动采集内容和用户编辑的旧笔记。不会自动重建被用户移走或删除的旧笔记。
- 相同 Markdown 字节内容位于不同路径时复用语义档案，并且只输出一份；不同路径的附件不会自动合并。原文版本变化可再次分析，但已有文章的副本更新须人工决定，程序报告冲突。
- 有合法分类的文章进入一级／二级目录，低置信度保留Front Matter中的待审核状态；没有合法目录的结果进入 `_待审核/未分类/待定`。人工与图片审核标签合并，明确标记“不纳入”的文章跳过。
- 本次复制失败但尚未成功输出的文章可再次运行补齐。临时文件先校验再独占发布，附件失败不会留下半成品；同名不同内容拒绝覆盖，同批目标重名只报告冲突，不阻塞其他文章。
- 每次复制产生独立SQLite记录和 `exports/INCREMENTAL_*.json` 清单；清单拒绝覆盖。规则改类不会自动搬迁旧副本。
- 生成新Obsidian副本时自动清理空白SVG占位图，转义表情图片的嵌套方括号；保留真实图片、正文、元数据和代码示例。复制清单与运行汇总记录清理数量和仍依赖网络的图片数量；远程下载失败不能靠分类恢复。已有笔记继续跳过，不自动改写。
- `copy-all` 继续用于首次空库复制，`copy-pilot` 继续保留原试运行语义；日常追加使用 `update` 或 `copy-new`。

## 第一阶段程序

程序只依赖 Python 3.11+ 和 PyYAML。先在项目目录执行：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

然后自行在 `.env` 中填写 `DEEPSEEK_API_KEY`。不要把密钥发送到聊天中。

`taxonomy.yaml` 中的三个目录路径是当前维护者的本机示例。其他电脑使用时，应在 `.env` 里设置 `SOURCE_LIBRARY`，并按本机情况修改项目路径和分类文章库路径。数据库、导出清单、试运行报告、文章标题汇总和 `.env` 均被 `.gitignore` 排除，不应提交到公开仓库。

常用命令：

```powershell
# 初始化项目内的 SQLite 数据库
python -m zhihu_classifier init-db

# 只读扫描 taxonomy.yaml 配置的原始文章库
python -m zhihu_classifier scan

# 查看扫描、分类和审核数量
python -m zhihu_classifier status

# 在单独的 PowerShell 面板中每10秒刷新分析进度
python -m zhihu_classifier watch --interval 10

# 每个内容哈希只阅读全文一次，同时生成永久语义档案和初始映射
python -m zhihu_classifier analyze --limit 10 --allow-draft

# 分类标准变化后只发送永久语义档案，不再发送原文
python -m zhihu_classifier remap --limit 100

# 修复失败结果：优先离线校正已保存 JSON；需要时再调用模型修复
python -m zhihu_classifier repair-saved --limit 100 --allow-draft
python -m zhihu_classifier repair-assignment-saved --limit 100 --allow-draft
python -m zhihu_classifier repair-analysis --limit 100 --allow-draft

# 只用永久语义档案复核候选主题，并生成质量报告
python -m zhihu_classifier recheck-candidates --limit 200 --allow-draft
python -m zhihu_classifier audit --sample-per-band 15

# 导出所有最新分类结果或仅导出待审核结果
python -m zhihu_classifier export
python -m zhihu_classifier export --review-only

# 导出置信度低于0.75的 Obsidian 人工审核清单
python -m zhihu_classifier export-review --below 0.75

# 导入已经全部定稿的低置信度审核；兼容旧分类在本地继承，不调用模型
python -m zhihu_classifier import-review --input reports\LOW_CONFIDENCE_REVIEW_1.0.md

# 只读识别正文极少但包含图片的文章，并生成人工看图清单
python -m zhihu_classifier export-visual-review --max-text-chars 200

# 导入人工看图结果；未填写项只有在显式确认后才沿用现有分类
python -m zhihu_classifier import-visual-review --accept-unfilled-current

# 仅在用户明确批准后，复制一个不超过10篇的分析批次
python -m zhihu_classifier copy-pilot --analysis-run-id 2 --confirm-copy

# 仅在用户明确批准全部文章且目标库为空时执行全量复制
python -m zhihu_classifier copy-all --confirm-copy-all
```

安全限制：

- `copy-pilot` 只处理不超过10篇的明确分析批次；`copy-all` 必须显式添加 `--confirm-copy-all`，首轮还要求分类文章库为空。程序没有移动、重命名或删除文章的命令。
- 未显式添加 `--allow-draft` 时，讨论中的分类规则不能调用模型分析或映射。
- 使用候选二级目录得到的结果，即使置信度较高，也会强制进入人工审核。
- API 空响应、JSON 截断、未知分类和格式错误均保存为错误记录，不会触发文件操作。
- `analyze` 以文章内容哈希去重；相同内容未变化时不会再次发送全文。
- `remap` 只读取项目数据库中的语义档案，不读取原始文章正文。
- `repair-saved` 和 `repair-assignment-saved` 只校正已保存 JSON 的层级与包装，不调用 API、不改写语义内容。
- `export-review` 生成带空白审核字段的 Markdown，若目标文件已存在会拒绝覆盖，以保护人工填写结果。
- `import-review` 逐条核对文章ID、标题、内容哈希和原分类，拒绝未定稿或重复导入；兼容旧版本映射只在本地继承，人工结果另存为可审计覆盖。
- `export-visual-review` 只读统计正文有效文字、图片引用和附件图片，生成独立审核清单；不会调用模型、复制图片或修改原始文章。
- `import-visual-review` 把人工分类覆盖、视觉摘要、收藏意图、保留决定和 `内容形态/图片为主` 标签保存到项目数据库；未填写项默认拒绝导入，必须由用户明确授权沿用现有分类。
- 正文没有足够可分类信息时，允许以空目录、置信度不高于0.2的形式保留在待审核队列；它不是正式类别，也不能自动复制到正式目录。
- `copy-pilot` 将 Markdown 和与其同名的图片目录作为文章包复制，逐文件校验哈希，以保持 Obsidian 相对图片引用有效。
- `copy-all` 使用每篇文章当前有效的正式分类，合并语义、图片审核和人工审核标签，直接写入一级／二级目录；待复核标记保留在Front Matter中，不改变当前目录。每篇正文与每个附件均在落盘后校验哈希，并记录到SQLite和JSON清单。
- 分类库中的 Markdown 副本会生成 Obsidian YAML Front Matter；原始 Markdown 不写入元数据，继续保持原样。
- 全文分析前只过滤无语义图片标记，不截断可读正文；评论区保留为次要语境。
- 每次调用的输入、缓存命中、缓存未命中、输出和总 token 都记录在 `api_usage`，可用 `status` 查看累计值。
- `analyze` 和 `remap` 默认逐篇显示完成比例、成功数与错误数；`watch` 可在独立 PowerShell 面板中显示数据库总进度，关闭面板不会停止后台分析。

## 版本约定

- 当前规则版本为 `1.1`；`1.0` 是首个正式基线，`1.1` 是首次增量发布。
- 文案、边界或少量二级分类调整，正式版以后递增次版本，例如 `1.0 → 1.1`。
- 一级目录或整体分类逻辑发生重大变化时递增主版本，例如 `1.x → 2.0`。
- 每次正式变化都记录到 `DECISIONS.md`。
- 未来每条分类结果必须保存所使用的规则版本。

## 当前状态

2330篇文章的永久语义档案和规则1.1分类均已完成。103篇低于0.75的文章已逐篇导入人工审核；用户又抽查了0.75–0.79的168篇，认为分类没有明显问题，并同意0.80–0.84区间先按当前分类试用。数据库仍保留542篇待复核标记，便于日后在Obsidian阅读中继续纠错。首次全量复制批次1已完成：新增2330篇Markdown和11435个同名附件目录内文件，共13765个目标文件；正文哈希、附件哈希及Front Matter复核均为0异常。复制过程未调用DeepSeek，未修改原始文章。
