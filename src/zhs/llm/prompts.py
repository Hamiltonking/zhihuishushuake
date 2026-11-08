"""LLM 答题 Prompt 模板与答案解析"""

import ast
import json
import re


def build_choice_prompt(
    question: str,
    choices: list[dict[str, object]],
    answer_type: str,
    reference_materials: list[dict[str, str]] | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """构建选择题/判断题 Prompt

    Args:
        question: 题目文本
        choices: 选项列表 [{"id": 1, "content": "A"}, ...]
        answer_type: 题目类型（"单选题" | "多选题" | "判断题"）
        reference_materials: 参考资料
        extra: 额外上下文（courseName, theme, knowledgePoint 等）
    """
    reference_materials = reference_materials or []
    extra = extra or {}

    parts: list[str] = []

    # 参考资料（markdown 代码块包裹，含文件名）
    if reference_materials:
        parts.append("参考资料：")
        for ref in reference_materials:
            name = ref.get("name", "")
            content = ref.get("content", "")
            parts.append(f"```{name}\n{content}\n```")

    # 上下文
    course_name = extra.get("courseName", "")
    theme = extra.get("theme", "")
    knowledge_point = extra.get("knowledgePoint", "")
    if course_name or theme or knowledge_point:
        ctx = f"假设你是一名学生，正在学习《{course_name}》。需要严格按照考试要求完成一道题目，否则无法及格。\n"
        if theme:
            ctx += f"现在，你学习到了{theme}。\n"
        if knowledge_point:
            ctx += f"本次考察知识点为{knowledge_point}。\n"
        parts.append(ctx)

    # 额外提示（排除选项、历史AI解析等）
    known_keys = {"courseName", "theme", "knowledgePoint"}
    for key, value in extra.items():
        if key not in known_keys and value:
            parts.append(f"【{key}】\n{value}")

    # 指令
    select_text = "所有正确的答案" if answer_type == "多选题" else "最合适的答案"
    instruction = (
        f"本题为{answer_type}，请从选项中选择{select_text}，回答放到markdown代码块中，例如：\n\n"
        "```answer\n"
        '[{"id": xxx, "content": xxx}]\n'
        "```\n"
        "答案必须为满足格式的json字符串(列表，单选也要是列表)，否则视为无效答案，不能得分。"
        "在这个markdown代码块（answer）外，你需要解释为什么你认为这个答案是正确的，"
        "并且标注出你选择这个答案的依据，这些依据必须有一定的权威性。（我提供给你的参考资料绝对权威可信）"
    )
    parts.append(instruction)

    # 题目
    parts.append(f"现在，请听题：\n\n{question}")

    # 选项
    parts.append(f"\n\n选项如下：```choices\n{json.dumps(choices, ensure_ascii=False, indent=4)}\n```")

    return "\n".join(parts)


def build_fill_blank_prompt(
    question: str,
    reference_materials: list[dict[str, str]] | None = None,
    extra: dict[str, str] | None = None,
) -> str:
    """构建填空题 Prompt

    Args:
        question: 题目文本
        reference_materials: 参考资料
        extra: 额外上下文
    """
    reference_materials = reference_materials or []
    extra = extra or {}

    parts: list[str] = []

    # 参考资料（markdown 代码块包裹，含文件名）
    if reference_materials:
        parts.append("参考资料：")
        for ref in reference_materials:
            name = ref.get("name", "")
            content = ref.get("content", "")
            parts.append(f"```{name}\n{content}\n```")

    # 上下文
    course_name = extra.get("courseName", "")
    theme = extra.get("theme", "")
    knowledge_point = extra.get("knowledgePoint", "")
    if course_name or theme or knowledge_point:
        ctx = f"假设你是一名学生，正在学习《{course_name}》。\n"
        if theme:
            ctx += f"现在，你学习到了{theme}。\n"
        if knowledge_point:
            ctx += f"本次考察知识点为{knowledge_point}。\n"
        parts.append(ctx)

    # 额外提示（排除选项、历史AI解析等）
    known_keys = {"courseName", "theme", "knowledgePoint"}
    for key, value in extra.items():
        if key not in known_keys and value:
            parts.append(f"【{key}】\n{value}")

    # 指令
    instruction = (
        "本题为填空题，请根据题目内容填写空白处的答案。如果有多个空，每个空的答案用换行分隔。"
        "回答放到markdown代码块中，例如：\n\n"
        "```answer\n"
        "第一个空的答案\n"
        "第二个空的答案\n"
        "```\n\n"
        "【填词规则】参考答案里通常就有原句，必须与原文用词严格一致：\n"
        "1. 答案必须是从参考资料中原样摘录的连续片段，逐字照抄，不得改写、缩写、扩写，也不得换成同义词。\n"
        "2. 按空位前后文裁剪长度：原文是“经济高质量发展”、题目是“对国家____发展…”，应填“经济高质量”（后面已有“发展”）。\n"
        "3. 不得省略原文中的限定词：原文是“基础理论研究”就不能只写“基础研究”，原文是“广泛协商”就不能只写“协商”。\n"
        "4. 不得添加原文没有的词：原文只写“人民”，就不要写成“人民群众”。\n"
        "5. 并列列举题（“气候变化、生物多样性丧失和____等”）要填原文并列项中的同类名词，"
        "只填名词，不要加“治理”“保护”“建设”“管理”等动词或后缀。\n"
        "6. 只有当参考资料完全没有相关内容时，才允许用自己的话概括。\n\n"
        "答案必须简洁准确，通常是词组或短句。"
        "在这个markdown代码块（answer）外，你需要解释为什么你认为这个答案是正确的，并指出它出自参考资料中的哪句话。"
    )
    parts.append(instruction)

    # 题目
    parts.append(f"现在，请听题：\n\n{question}")

    return "\n".join(parts)


def build_fill_blank_repair_prompt(
    question: str,
    current_answers: list[str],
    reference_excerpt: str,
) -> str:
    """构建填空题「原文对齐」修复 Prompt

    模型第一次给的答案常常是近义概括（把「经济高质量」写成「经济」），
    这里用一次额外调用，把它改写成参考资料里逐字一致的片段。

    Args:
        question: 题目文本（含空白）
        current_answers: 当前答案（按空顺序）
        reference_excerpt: 从参考资料中筛出的、与题目最相关的原文片段
    """
    answers_block = "\n".join(f"- 第{i}空：{answer}" for i, answer in enumerate(current_answers, 1))
    return (
        "下面是一道填空题、你之前的答案，以及教材原文片段。\n\n"
        f"题目：{question}\n\n"
        f"你之前的答案：\n{answers_block}\n\n"
        f"教材原文：\n```原文\n{reference_excerpt}\n```\n\n"
        "请只依据上面的原文，把每个空的答案改成与原文完全一致的说法：\n"
        "1. 逐字摘录原文片段作为答案，不得改写、缩写或换成同义词；\n"
        "2. 按空位前后文裁剪长度（原文“经济高质量发展”+题干“…____发展…”→“经济高质量”）；\n"
        "3. 不得增删原文中的限定词（“基础理论研究”不能写成“基础研究”，“广泛协商”不能写成“协商”）；\n"
        "4. 若题目是并列列举（“A、B和____等”），答案必须是原文并列项中的同类名词，"
        "不得添加“治理”“保护”“建设”“管理”等动词或后缀；\n"
        "5. 原文确实找不到对应内容时，保留原答案。\n\n"
        "按空顺序输出，每个空一行，放到markdown代码块中，例如：\n\n"
        "```answer\n"
        "第一个空的答案\n"
        "第二个空的答案\n"
        "```\n\n"
        "代码块之外不要输出任何内容。"
    )


_BLANK_RE = re.compile(r"[_\uff3f\u2014]{2,}|\uff08\s*\uff09|\(\s*\)")


def count_blanks(question: str) -> int:
    """统计题干里的空白个数（____ / （ ） / ( )）"""
    return len(_BLANK_RE.findall(question))


def parse_choice_answer(completion: str) -> list[int]:
    """从 LLM 输出提取选项 ID 列表

    解析 ```answer\\n[{"id": 1, "content": "A"}]\\n``` 格式的输出。
    支持 JSON 和 ast.literal_eval 两种解析方式。
    """
    match = re.search(r"```answer\s*\n(.*?)\n\s*```", completion, re.DOTALL)
    if not match:
        return []

    content = match.group(1).strip()
    if not content:
        return []

    # 先尝试 JSON，再兜底 ast.literal_eval
    try:
        items = json.loads(content)
    except json.JSONDecodeError:
        try:
            items = ast.literal_eval(content)
        except (ValueError, SyntaxError):
            return []

    if not isinstance(items, list):
        return []

    # 兼容两种写法：[{"id": 123, ...}] 与 [123, 456]
    ids: list[int] = []
    for item in items:
        if isinstance(item, dict) and "id" in item:
            ids.append(int(item["id"]))
        elif isinstance(item, int):
            ids.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            ids.append(int(item.strip()))
    return ids


def parse_fill_blank_answer(completion: str, expected: int = 0) -> list[str]:
    """从 LLM 输出提取填空答案

    解析 ```answer\\n答案1\\n答案2\\n``` 格式的输出，按行提取。
    每个空的答案作为列表的一个元素，返回 ["答案1", "答案2"]。
    提交时由 worker 用 / 拼接为 "答案1/答案2"。

    Args:
        completion: 模型输出
        expected: 题干里的空白个数。模型有时把多个空写在一行并用分隔符连接，
            此时按行只能取到 1 个，会在 expected > 1 时尝试按分隔符拆分。
    """
    match = re.search(r"```answer\s*\n(.*?)\n\s*```", completion, re.DOTALL)
    if not match:
        return []

    content = match.group(1).strip()
    if not content:
        return []

    answers = [line.strip() for line in content.split("\n") if line.strip()]

    # 多个空却只写在一行：尝试按常见分隔符拆分（仅当拆出来的个数正好对得上）
    if expected > 1 and len(answers) == 1:
        for separator in ("/", "；", ";", "、", "|"):
            parts = [part.strip() for part in answers[0].split(separator) if part.strip()]
            if len(parts) == expected:
                return parts
    return answers
