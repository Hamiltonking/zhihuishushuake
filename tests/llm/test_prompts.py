"""Task 5.1 — llm/prompts.py TDD"""

from zhs.llm.prompts import (
    build_choice_prompt,
    build_fill_blank_prompt,
    parse_choice_answer,
    parse_fill_blank_answer,
)


class TestBuildChoicePrompt:
    """build_choice_prompt 选择题/判断题 Prompt"""

    def test_contains_answer_marker(self) -> None:
        """选择题 Prompt 包含 ```answer``` 标记"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}, {"id": 2, "content": "动物"}],
            answer_type="单选题",
        )
        assert "```answer" in prompt

    def test_contains_choices_marker(self) -> None:
        """选择题 Prompt 包含 ```choices``` 标记"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}, {"id": 2, "content": "动物"}],
            answer_type="单选题",
        )
        assert "```choices" in prompt

    def test_contains_question(self) -> None:
        """Prompt 包含题目文本"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}],
            answer_type="单选题",
        )
        assert "什么是 Python?" in prompt

    def test_contains_answer_type(self) -> None:
        """Prompt 包含题目类型"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}],
            answer_type="多选题",
        )
        assert "多选题" in prompt

    def test_reference_materials_included(self) -> None:
        """参考资料被包含在 Prompt 中"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}],
            answer_type="单选题",
            reference_materials=[{"title": "参考", "content": "Python 是一种编程语言"}],
        )
        assert "Python 是一种编程语言" in prompt

    def test_extra_context_included(self) -> None:
        """extra 中的上下文信息被包含"""
        prompt = build_choice_prompt(
            question="什么是 Python?",
            choices=[{"id": 1, "content": "编程语言"}],
            answer_type="单选题",
            extra={"courseName": "计算机基础", "knowledgePoint": "编程语言"},
        )
        assert "计算机基础" in prompt
        assert "编程语言" in prompt


class TestBuildFillBlankPrompt:
    """build_fill_blank_prompt 填空题 Prompt"""

    def test_contains_answer_marker(self) -> None:
        """填空题 Prompt 包含 ```answer``` 标记"""
        prompt = build_fill_blank_prompt(question="Python 是一种___语言")
        assert "```answer" in prompt

    def test_contains_question(self) -> None:
        """Prompt 包含题目文本"""
        prompt = build_fill_blank_prompt(question="Python 是一种___语言")
        assert "Python 是一种___语言" in prompt

    def test_reference_materials_included(self) -> None:
        """参考资料被包含"""
        prompt = build_fill_blank_prompt(
            question="Python 是一种___语言",
            reference_materials=[{"title": "参考", "content": "Python 是编程语言"}],
        )
        assert "Python 是编程语言" in prompt


class TestParseChoiceAnswer:
    """parse_choice_answer 从 LLM 输出提取选项 ID"""

    def test_json_format(self) -> None:
        """正常 JSON 格式解析"""
        completion = '```answer\n[{"id": 1, "content": "A"}, {"id": 3, "content": "C"}]\n```'
        result = parse_choice_answer(completion)
        assert result == [1, 3]

    def test_literal_eval_fallback(self) -> None:
        """异常格式 → ast.literal_eval 兜底"""
        completion = "```answer\n[{'id': 1, 'content': 'A'}]\n```"
        result = parse_choice_answer(completion)
        assert result == [1]

    def test_single_choice(self) -> None:
        """单选题只有一个选项"""
        completion = '```answer\n[{"id": 2, "content": "B"}]\n```'
        result = parse_choice_answer(completion)
        assert result == [2]

    def test_no_answer_block(self) -> None:
        """无 answer 代码块 → 空列表"""
        result = parse_choice_answer("没有答案")
        assert result == []


class TestParseFillBlankAnswer:
    """parse_fill_blank_answer 从 LLM 输出提取填空答案"""

    def test_multiple_lines(self) -> None:
        """多行答案：每行一个元素（每空一个）"""
        completion = "```answer\n答案1\n答案2\n```"
        result = parse_fill_blank_answer(completion)
        assert result == ["答案1", "答案2"]

    def test_empty_output(self) -> None:
        """空输出"""
        result = parse_fill_blank_answer("```answer\n```")
        assert result == []

    def test_single_line(self) -> None:
        """单行答案"""
        completion = "```answer\n唯一答案\n```"
        result = parse_fill_blank_answer(completion)
        assert result == ["唯一答案"]

    def test_no_answer_block(self) -> None:
        """无 answer 代码块 → 空列表"""
        result = parse_fill_blank_answer("没有答案")
        assert result == []


class TestParseFillBlankMultipleBlanks:
    """多空填空题的答案切分"""

    def test_split_single_line_by_slash(self) -> None:
        """多个空写在一行用 / 分隔时按空数拆分"""
        out = parse_fill_blank_answer("```answer\n领导体制/决策机制\n```", expected=2)
        assert out == ["领导体制", "决策机制"]

    def test_no_split_when_counts_mismatch(self) -> None:
        """拆分后个数对不上就保持原样"""
        out = parse_fill_blank_answer("```answer\n领导体制/决策机制\n```", expected=3)
        assert out == ["领导体制/决策机制"]

    def test_single_blank_keeps_punctuation(self) -> None:
        """只有一个空时不做任何拆分"""
        out = parse_fill_blank_answer("```answer\n富强、民主\n```", expected=1)
        assert out == ["富强、民主"]


class TestParseChoiceAnswerPlainIds:
    """多选题纯 id 数组的兼容"""

    def test_plain_int_ids(self) -> None:
        """[123, 456] 这种纯 id 数组也能解析"""
        assert parse_choice_answer("```answer\n[123, 456]\n```") == [123, 456]

    def test_string_ids(self) -> None:
        """字符串形式的 id 也能解析"""
        assert parse_choice_answer("```answer\n[\"123\", \"456\"]\n```") == [123, 456]
