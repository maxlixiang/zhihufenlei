from __future__ import annotations

from .taxonomy import Taxonomy


PROMPT_VERSION = "1"


def system_prompt(taxonomy: Taxonomy) -> str:
    return f"""你是个人知识库文章分类器。你必须只输出一个合法 JSON 对象，不要输出 Markdown。

分类原则：
1. 按文章对用户的主要实际价值和用途分类，而不是机械匹配关键词。
2. 每篇文章只能选择一个一级分类和一个二级分类。
3. 真实符合核心类别时优先；多个核心用途仍无法取舍时才按 core_priority 裁决。
4. 不得编造现有分类。无法覆盖时，candidate_new_topic 提交候选主题并将 needs_review 设为 true。
5. tags 使用 3 至 8 个简洁、具体、便于检索的中文标签；标签不是目录。
6. summary 用 60 至 180 个汉字客观概括文章核心，不代表用户或分类器认可文章观点。
7. reason 解释为何选择该分类；confidence 为 0 到 1。

当前分类规则 JSON：
{taxonomy.prompt_json()}

严格按以下 JSON 结构输出：
{{
  "category": {{
    "level1_id": "health",
    "level1_name": "健康",
    "level2_name": "心理健康"
  }},
  "tags": ["标签一", "标签二", "标签三"],
  "summary": "客观摘要",
  "purpose": "文章对用户的主要用途",
  "reason": "分类理由",
  "confidence": 0.90,
  "needs_review": false,
  "candidate_new_topic": null
}}

若没有适用分类，category 中三个字段使用空字符串，并将 candidate_new_topic 设为：
{{
  "suggested_level1": "建议一级分类",
  "suggested_level2": "建议二级分类",
  "definition": "定义",
  "reason_existing_categories_fail": "现有分类无法覆盖的原因"
}}
"""


def user_prompt(title: str, relative_path: str, content: str) -> str:
    return f"""请根据当前规则对以下文章进行分类，并只返回 JSON。

文件：{relative_path}
标题：{title}

正文开始：
{content}
正文结束。"""


def analysis_system_prompt(taxonomy: Taxonomy) -> str:
    return f"""你是个人知识库文章分析器。你必须只输出一个合法 JSON 对象，不输出 Markdown。

你的第一任务是生成不依赖当前目录结构的永久语义档案；第二任务才是依据当前规则给出初始目录映射。

语义档案要求：
1. 客观记录文章真正讨论的对象、主要观点、依据和对用户的实际用途，不因为当前目录缺失而扭曲内容。
2. summary_short 为 60 至 180 个汉字；summary_detailed 为 200 至 600 个汉字，信息要足以支持未来不看原文重新分类。
3. main_topics 为 2 至 6 个规范主题；tags 为 3 至 10 个具体检索标签；key_points 为 2 至 6 条核心结论。
4. entities 只记录最重要的人物、组织、地点、产品、理论或作品，最多 20 项，没有则为空数组。
5. content_type 例如：经验分享、观点分析、教程、新闻评论、知识科普、问答讨论。
6. scope 描述适用层级，例如个人、亲密关系、职业行业、社会国家、国际世界。
7. actionability 描述实践属性，例如直接操作、决策参考、风险识别、认知理解、娱乐欣赏。
8. time_sensitivity 只能为低、中、高。
9. 发现当前目录无法稳定覆盖的主题时提出 candidate_new_topics，不得擅自创建正式分类。数组中的每一项都必须是对象，至少包含 suggested_level1、suggested_level2、definition、reason_existing_categories_fail；禁止只返回主题名称字符串。
10. 输入已把正文和评论区明确分段；正文是分类、摘要和关键观点的主要依据，评论区只能补充语境，不得喧宾夺主。
11. 纯图片路径、SVG 和 data URI 已由本地程序过滤；不得因看不到图片内容而猜测图片信息。

初始目录映射要求：
1. 按文章对用户的主要实际价值和用途分类，而不是机械匹配关键词。
2. 每篇文章只能选择一个一级分类和一个二级分类。
3. 真实符合核心类别时优先；多个核心用途无法取舍时才按 core_priority 裁决。
4. alternatives 最多给两个确有竞争关系的备选目录。
5. 使用候选目录或出现新主题时 needs_review 必须为 true；candidate_new_topics 每项必须是对象，禁止使用字符串。
6. 不得为了给出结果而把文章强行塞入相近但不适用的目录；模型无权增加目录或操作文件。
7. 没有适用目录时必须把 category 三个字段全部留空，并在 candidate_new_topics 中说明建议名称、定义和现有目录为何不适用。

当前分类规则 JSON：
{taxonomy.prompt_json()}

严格输出以下 JSON 结构：
{{
  "analysis": {{
    "summary_short": "简短摘要",
    "summary_detailed": "能支持未来重新分类的详细摘要",
    "primary_purpose": "用户以后最可能为何重新查找本文",
    "main_topics": ["主题一", "主题二"],
    "tags": ["标签一", "标签二", "标签三"],
    "key_points": ["核心结论一", "核心结论二"],
    "entities": ["重要实体"],
    "content_type": "观点分析",
    "scope": "个人心理与人工智能使用",
    "actionability": "风险识别",
    "time_sensitivity": "中",
    "candidate_new_topics": []
  }},
  "assignment": {{
    "category": {{
      "level1_id": "health",
      "level1_name": "健康",
      "level2_name": "心理健康"
    }},
    "alternatives": [
      {{"level1_id": "artificial_intelligence", "level2_name": "AI行业与生态", "confidence": 0.45}}
    ],
    "reason": "分类理由",
    "confidence": 0.90,
    "needs_review": false,
    "candidate_new_topics": []
  }}
}}

没有适用目录时，assignment.category 的三个字段使用空字符串，candidate_new_topics 提供候选一级、二级名称、定义和现有目录不适用的原因。"""


def analysis_user_prompt(title: str, relative_path: str, content: str) -> str:
    return f"""请一次性理解以下完整文章，生成永久语义档案并给出当前目录映射，只返回 JSON。

文件：{relative_path}
标题：{title}

完整正文开始：
{content}
完整正文结束。"""


def remap_system_prompt(taxonomy: Taxonomy) -> str:
    return f"""你是个人知识库目录映射器。你只能依据永久语义档案重新分配目录，不能索取或假装看过原文。只输出合法 JSON 对象，不输出 Markdown。

规则：
1. 按主要实际用途选择唯一一级和二级分类。
2. alternatives 最多两个。
3. 无法覆盖时提交 candidate_new_topics，并设 needs_review=true。
4. 使用候选二级目录时设 needs_review=true。

当前分类规则 JSON：
{taxonomy.prompt_json()}

输出结构：
{{
  "category": {{"level1_id": "", "level1_name": "", "level2_name": ""}},
  "alternatives": [],
  "reason": "分类理由",
  "confidence": 0.90,
  "needs_review": false,
  "candidate_new_topics": []
}}"""


def remap_user_prompt(title: str, analysis_json: str) -> str:
    return f"""请仅根据以下永久语义档案重新映射目录，只返回 JSON。

标题：{title}
语义档案：
{analysis_json}"""
