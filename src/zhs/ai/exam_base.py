"""AI 作业/考试基类（模板方法模式）

封装 HomeworkCtx 与 ExamCtx 的公共流程与状态：
- LLM 提供者初始化（通过 LLMProviderFactory）
- 单级内存缓存（_answer_cache，当前 exam），持久化通过 AiExamCache
- 三级答案策略（缓存 → AI → 随机）
- 心跳（daemon 线程）
- 进度条更新
- 结果检查与缓存更新

缓存查询顺序（_get_cached_answer）：
1. _answer_cache（当前 exam 内存缓存）
2. cache.get（当前 exam SQLite 查询）
3. cache.search_in_course（跨 exam 课程级查询，仅 ExamCtx 启用）

子类只需实现差异化的抽象方法（_open / _get_sheet_content / _save_answer 等），
并通过 _cross_exam_search 属性控制是否启用跨 exam 查询。
"""

import contextlib
import random
import re
import threading
import time
from abc import ABC, abstractmethod
from typing import Any

from loguru import logger

from zhs.ai.models import QuestionContent, QuestionSheet
from zhs.api.web_search import WebSearchClient
from zhs.cache.ai_cache import AiExamCache
from zhs.config import AIConfig
from zhs.exceptions import ZhsError
from zhs.llm.base import LLMProvider
from zhs.llm.factory import LLMProviderFactory
from zhs.llm.prompts import build_fill_blank_repair_prompt
from zhs.question_bank.client import QuestionBankClient
from zhs.question_bank.models import map_question_type
from zhs.reporter import ConsoleReporter, ProgressReporter
from zhs.session import ZhsSession
from zhs.utils.display import _C, progress_bar, styled
from zhs.utils.html import extract_text


class AiExamBase(ABC):
    """AI 作业/考试基类（模板方法模式）

    子类 HomeworkCtx（逐题保存）与 ExamCtx（批量保存）共享此基类。
    """

    def __init__(
        self,
        session: ZhsSession,
        course_id: int | str,
        exam_test_id: int | str,
        exam_paper_id: int | str,
        ai_config: AIConfig,
        op_extra: dict[str, Any] | None = None,
        progress_view: bool = True,
        reporter: ProgressReporter | None = None,
        cache: AiExamCache | None = None,
        question_bank: QuestionBankClient | None = None,
    ) -> None:
        self._session = session
        self._course_id = course_id
        self._exam_test_id = exam_test_id
        self._exam_paper_id = exam_paper_id
        self._ai_config = ai_config
        self._op_extra = op_extra or {}
        self._progress_view = progress_view
        self._reporter = reporter or ConsoleReporter()
        self._cache = cache or AiExamCache()
        self._question_bank = question_bank

        # 缓存（当前 exam 内存缓存，持久化通过 self._cache）
        self._answer_cache: dict[str, dict[str, Any]] = {}

        # 联网搜索（开启时按题干补充参考资料）
        self._web_search = WebSearchClient.from_config(ai_config)
        self._web_materials: dict[int, list[dict[str, str]]] = {}

        # 状态
        self._sheet_content: list[QuestionSheet] | None = None
        self._stopped = False
        self._heartbeat_thread: threading.Thread | None = None
        self._reference_materials: list[dict[str, str]] = []

        # 进度
        self._progress_total = 0
        self._progress_current = 0
        self._progress_sources: list[str] = []

        # LLM 提供者（统一通过工厂创建）
        self._provider: LLMProvider | None = LLMProviderFactory.create(
            ai_config,
            session=session,
            course_id=str(course_id),
            course_name=str(self._op_extra.get("courseName", "")) if self._op_extra else "",
        )

    # --- 模板方法 ---

    def start(
        self,
        reference_materials: list[dict[str, str]] | None = None,
        submit: bool = False,
    ) -> tuple[bool, int, int]:
        """执行完整流程，返回 (是否全对, 正确数, 总题数)

        Args:
            reference_materials: 参考资料（PPT 等）
            submit: 是否提交（子类决定是否使用）
        """
        self._reference_materials = reference_materials or []

        self._load_cache()
        self._open()

        # 启动心跳（daemon 线程）
        self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._heartbeat_thread.start()

        sheets = self._get_sheet_content()
        if not sheets:
            self._stopped = True
            raise ZhsError("答题卡内容为空")

        # 答题
        total = len(sheets)
        self._progress_total = total
        self._progress_current = 0
        self._progress_sources = []
        self._update_progress("pending")
        self._answer_questions(sheets)

        self._reporter.wipe_line()

        # 提交与结果检查（子类决定具体行为）
        result = self._finish(submit, sheets)
        self._stopped = True
        return result

    def _finish(self, submit: bool, sheets: list[QuestionSheet]) -> tuple[bool, int, int]:
        """结束流程（提交 + 结果检查）。默认实现：不提交，返回 (False, 0, total)。

        子类可重写以实现提交逻辑。
        """
        return False, 0, len(sheets)

    # --- 公共实现（子类一般不重写）---

    @property
    @abstractmethod
    def _exam_base_url(self) -> str:
        """考试 API 基础 URL"""
        ...

    @abstractmethod
    def _api_query(self, url: str, data: dict[str, Any], method: str = "POST") -> dict[str, Any]:
        """同步 API 查询（子类决定使用哪个 session 方法）"""
        ...

    @abstractmethod
    def _open(self) -> None:
        """打开作业/考试"""
        ...

    @abstractmethod
    def _get_sheet_content(self) -> list[QuestionSheet]:
        """获取答题卡内容"""
        ...

    @abstractmethod
    def _get_question_content(self, question_id: int, version: int) -> QuestionContent | None:
        """获取题目详情"""
        ...

    @abstractmethod
    def _save_answer(self, question_id: int, answers: list[str]) -> bool:
        """保存单题答案"""
        ...

    @abstractmethod
    def _submit(self, submit: bool) -> None:
        """提交"""
        ...

    @abstractmethod
    def _answer_questions(self, sheets: list[QuestionSheet]) -> None:
        """答题流程（子类决定逐题或批量）"""
        ...

    # --- 缓存键 ---

    @staticmethod
    def _cache_key(question_id: int) -> str:
        """生成缓存键（仅用 question_id，不含 version）"""
        return str(question_id)

    # --- 跨 exam 查询开关 ---

    @property
    def _cross_exam_search(self) -> bool:
        """是否启用跨 exam 课程级查询（ExamCtx 重写为 True，HomeworkCtx 默认 False）

        - homework 仅查当前 exam（作业针对单个考试，题目已知）
        - exam 可跨 exam 搜索课程题库（考试考察课程全部知识，需复用其他 exam 的答案）
        """
        return False

    # --- 缓存查询 ---

    def _get_cached_answer(self, question_id: int, question_text: str = "") -> list[str] | None:
        """从缓存获取答案（三级查询）

        查询顺序：
        1. _answer_cache（当前 exam 内存缓存）
        2. cache.get（当前 exam SQLite 查询，内存未命中时的安全兜底）
        3. cache.search_in_course（跨 exam 课程级查询，仅当 question_text 非空）

        Args:
            question_id: 题目 ID
            question_text: 题目文本（清洗后），用于跨 exam 搜索。为空则不跨 exam 搜索。
        """
        key = self._cache_key(question_id)

        # 1. 当前 exam 内存缓存
        entry = self._answer_cache.get(key)
        if entry is not None:
            return self._parse_cached_answer(entry.get("answer", ""))

        # 2. 当前 exam SQLite 查询（内存未命中时的安全兜底，回填内存缓存）
        entry = self._cache.get(self._course_id, self._exam_test_id, question_id)
        if entry is not None:
            self._answer_cache[key] = entry
            return self._parse_cached_answer(entry.get("answer", ""))

        # 3. 跨 exam 课程级查询（仅 exam 模块，question_text 非空时触发）
        if question_text:
            entry = self._cache.search_in_course(self._course_id, question_text)
            if entry is not None:
                self._answer_cache[key] = entry
                return self._parse_cached_answer(entry.get("answer", ""))

        return None

    @staticmethod
    def _parse_cached_answer(answer_str: str) -> list[str] | None:
        """解析缓存中的 answer 字段（委托给 AiExamCache.parse_answer）

        - 含 #@# → 按 #@# 分隔（多选题选项 ID）
        - 不含 #@# → 返回单元素列表（单选/判断/填空题）
        填空题多个空用 / 合并存储，不拆分。
        """
        return AiExamCache.parse_answer(answer_str)

    def _set_cached_answer(self, question_id: int, data: dict[str, Any]) -> None:
        """设置缓存（写入当前 exam 内存缓存，_save_cache 时持久化到 SQLite）"""
        key = self._cache_key(question_id)
        cache_entry = {
            "question": data.get("question", ""),
            "answer": data.get("answer", ""),
            "answer_content": data.get("answer_content", ""),
            "questionDict": data.get("questionDict", {}),
        }
        self._answer_cache[key] = cache_entry

    # --- 三级答案策略 ---

    def _get_answer(self, question: QuestionContent) -> tuple[list[str], str]:
        """获取答案，返回 (答案列表, 来源标记)

        三级策略：1. 缓存命中 2. AI 生成 3. 兜底随机
        """
        question_id = question.id
        question_type = question.question_type

        # 1. 查缓存（exam 模块启用跨 exam 搜索，传清洗后的题目文本）
        question_text = extract_text(question.content) if self._cross_exam_search else ""
        cached = self._get_cached_answer(question_id, question_text)
        if cached is not None:
            if self._is_cache_compatible(question, cached):
                return cached, "cached"
            # 跨卷复用是按题干匹配的：题目相同但选项 ID 变了（换卷/重新生成）时
            # 旧答案里的 ID 在新卷里根本不存在，直接提交会答错，所以丢弃重答。
            logger.info(f"缓存答案与当前选项不匹配，改为重新作答（题目 {question_id}）：{cached}")

        # 选项
        choices = [{"id": opt.id, "content": opt.content} for opt in question.option_vos]

        # 选项少于 2 个且非填空 → 选第一个
        if len(choices) < 2 and question_type != 3:
            return [str(choices[0]["id"])], "cached"

        # 2. AI 生成
        if self._provider is not None:
            extra = self._build_extra(question, choices)
            try:
                # 所有题型统一：本地参考资料 + 联网搜索结果一起喂给模型
                materials = self._question_materials(question)

                if question_type == 1:
                    ids = self._provider.single_choice(question.content, choices, materials, extra)
                    if ids:
                        return [str(i) for i in ids], "AI generated"
                    logger.warning(f"{question.content} {question_id} AI provide empty")
                elif question_type == 2:
                    ids = self._provider.multiple_choice(question.content, choices, materials, extra)
                    if ids:
                        return [str(i) for i in ids], "AI generated"
                elif question_type == 14:
                    ids = self._provider.judgement(question.content, choices, materials, extra)
                    if ids:
                        return [str(i) for i in ids], "AI generated"
                elif question_type == 3:
                    answers = self._provider.fill_blank(question.content, materials, extra)
                    if answers:
                        return self._anchor_fill_blank(question.content, answers, materials), "AI generated"
            except Exception as e:
                logger.error(f"AI 生成答案失败: {e}")

        # 3. 兜底随机
        if question_type == 3:
            return ["未知"], "random"
        elif question_type == 1 and choices:
            return [str(random.choice(choices)["id"])], "random"
        elif question_type == 2 and choices:
            n = min(2, len(choices))
            selected = random.sample(choices, n)
            return [str(c["id"]) for c in selected], "random"
        elif question_type == 14 and choices:
            return [str(random.choice(choices)["id"])], "random"
        return [], "random"

    @staticmethod
    def _is_cache_compatible(question: QuestionContent, answers: list[str]) -> bool:
        """缓存里的答案能否直接用于当前题目

        选择题/判断题的答案是选项 ID，跨卷复用必须确认这些 ID 在当前题目里都存在；
        填空题答案是文本，永远兼容。
        """
        if question.question_type not in (1, 2, 14):
            return True
        valid_ids = {str(option.id) for option in question.option_vos}
        return bool(answers) and all(answer in valid_ids for answer in answers)

    # --- 填空题原文对齐 ---

    _FILL_BLANK_TRIM = " \t\r\n\u201c\u201d\"'\u2018\u2019\u300a\u300b<>()\uff08\uff09[]\u3010\u3011"
    _FILL_BLANK_TAIL = "\u3002\uff0e.\uff1b;\uff0c,"
    _BLANK_PATTERN = re.compile(r"[_\uff3f\u2014]{2,}|\uff08\s*\uff09|\(\s*\)")

    def _question_materials(self, question: QuestionContent) -> list[dict[str, str]]:
        """当前题目的参考资料：课程文本（PPT/文本资源）+ 联网搜索结果，两者合并

        本地资料更贴近课程原话，排在前面；联网结果作为补充，排在后面。
        联网结果按题目缓存，避免同一题重复搜索。
        """
        materials = list(self._reference_materials)
        if self._web_search is None:
            return materials

        if question.id not in self._web_materials:
            self._web_materials[question.id] = self._web_search.build_materials(extract_text(question.content))
        web_materials = self._web_materials[question.id]
        if web_materials:
            logger.info(
                f"参考资料合并：本地 {len(materials)} 条 + 联网 {len(web_materials)} 条（题目 {question.id}）"
            )
        return materials + web_materials

    @staticmethod
    def _normalize_for_match(text: str) -> str:
        """去掉空白与引号括号，保留标点（用作句界）"""
        return re.sub(r"[\s\u3000\u201c\u201d\"'\u2018\u2019\u300a\u300b<>()\uff08\uff09\[\]\u3010\u3011]", "", text)

    def _clean_fill_blank(self, text: str) -> str:
        """清洗单个填空答案：去首尾引号、括号、句末标点"""
        cleaned = text.strip().strip(self._FILL_BLANK_TRIM)
        return cleaned.rstrip(self._FILL_BLANK_TAIL).strip()

    @staticmethod
    def _reference_text(materials: list[dict[str, str]]) -> str:
        """参考资料纯文本"""
        return "\n".join(str(ref.get("content", "")) for ref in materials)

    def _is_grounded_in_reference(self, answers: list[str], materials: list[dict[str, str]]) -> bool:
        """答案是否都能在参考资料原文中原样找到（无参考资料时视为通过）"""
        haystack = self._normalize_for_match(self._reference_text(materials))
        if not haystack:
            return True
        for answer in answers:
            needle = self._normalize_for_match(answer)
            if needle and needle not in haystack:
                return False
        return True

    def _reference_excerpt(self, question: str, materials: list[dict[str, str]], limit: int = 5) -> str:
        """按题干预原文的字符重合度，挑出最相关的几句原文作为改写依据"""
        text = self._reference_text(materials)
        if not text:
            return ""
        key = self._normalize_for_match(re.sub(r"[_\uff3f\u2014\s]+", "", question))
        units = [u.strip() for u in re.split(r"[\n\u3002\uff1b;]", text) if u.strip()]
        scored: list[tuple[int, int, str]] = []
        for idx, unit in enumerate(units):
            normalized = self._normalize_for_match(unit)
            hits = sum(1 for i in range(len(key) - 1) if key[i : i + 2] in normalized)
            scored.append((hits, idx, unit))
        scored.sort(key=lambda item: (-item[0], item[1]))
        picked = [unit for hits, _, unit in scored[:limit] if hits > 0]
        return "\n".join(picked)

    def _split_by_blanks(self, question: str) -> list[str] | None:
        """按空白标记切分题干；没有空白时返回 None"""
        segments = self._BLANK_PATTERN.split(question)
        if len(segments) < 2:
            return None
        return [self._normalize_for_match(segment) for segment in segments]

    def _reconstruct_answers(
        self, question: str, materials: list[dict[str, str]], max_answer_len: int = 30
    ) -> list[str] | None:
        """用参考资料里的原句把每个空的答案“卡”出来

        作业题干通常直接摘自教材：把空白两侧的文字当锚点，在原文里定位句子，
        两段锚点之间的原文片段就是该填的内容。既能把「经济」补成「经济高质量」，
        也能把「人民群众」收窄成原文的「人民」。
        """
        segments = self._split_by_blanks(question)
        if segments is None or any(not segment for segment in segments):
            return None
        haystack = self._normalize_for_match(self._reference_text(materials))
        if not haystack:
            return None

        head = segments[0]
        start = haystack.find(head)
        while start != -1:
            cursor = start + len(head)
            picked: list[str] = []
            matched = True
            for nxt in segments[1:]:
                window_end = min(len(haystack), cursor + max_answer_len)
                offset = haystack.find(nxt, cursor, window_end)
                if offset == -1:
                    matched = False
                    break
                picked.append(haystack[cursor:offset])
                cursor = offset + len(nxt)
            if matched and len(picked) == len(segments) - 1 and all(picked):
                return picked
            start = haystack.find(head, start + 1)
        return None

    def _anchor_fill_blank(
        self,
        question: str,
        answers: list[str],
        materials: list[dict[str, str]] | None = None,
    ) -> list[str]:
        """把填空答案对齐到参考资料原文

        模型爱给近义概括（「经济」「基础研究」「协商」），而服务端按原文用词判分。
        优先用题干锚点在原文中还原整句（确定性，无额外模型调用）；
        还原失败时再退回一次 LLM 改写。
        """
        refs = self._reference_materials if materials is None else materials
        local = self._reference_materials
        cleaned = [self._clean_fill_blank(answer) for answer in answers]
        cleaned = [answer for answer in cleaned if answer]
        if not cleaned:
            return answers
        if not refs:
            return cleaned

        # 还原优先用课程原文；课程原文匹配不到时再试合并资料
        reconstructed = self._reconstruct_answers(question, local) if local else None
        if reconstructed is None:
            reconstructed = self._reconstruct_answers(question, refs)
        if reconstructed is not None and len(reconstructed) == len(cleaned):
            if reconstructed != cleaned:
                logger.info(f"填空题答案按原文还原: {cleaned} -> {reconstructed}")
            return reconstructed

        # 改写只依据课程原文：联网摘要噪声大，实测会越改越偏
        if not local:
            logger.info(f"没有本地参考资料，跳过填空题改写: {cleaned}")
            return cleaned
        if self._provider is None:
            logger.warning(f"填空题答案不在参考资料原文中: {cleaned}")
            return cleaned

        excerpt = self._reference_excerpt(question, local) or self._reference_excerpt(question, refs)
        if not excerpt:
            logger.warning(f"参考资料中找不到与题目相关的句子，填空题答案保持原样: {cleaned}")
            return cleaned

        try:
            raw = self._provider.completion(build_fill_blank_repair_prompt(question, cleaned, excerpt))
            repaired = [
                self._clean_fill_blank(answer)
                for answer in self._provider.parse_fill_blank_answer(raw, expected=len(cleaned))
            ]
            repaired = [answer for answer in repaired if answer]
        except Exception as e:
            logger.warning(f"填空题答案对齐失败，保留原答案: {e}")
            return cleaned

        if len(repaired) != len(cleaned):
            logger.warning(f"填空题对齐后数量不一致，保留原答案: {cleaned} -> {repaired}")
            return cleaned
        if repaired != cleaned:
            logger.info(f"填空题答案按原文校正: {cleaned} -> {repaired}")
        if not self._is_grounded_in_reference(repaired, refs):
            logger.warning(f"填空题对齐后仍不在原文中: {repaired}")
        return repaired

    # --- 题库参考注入 ---

    def _build_extra(self, question: QuestionContent, choices: list[dict[str, Any]]) -> dict[str, Any]:
        """构建 extra 字典：拷贝 _op_extra，并在题库命中时注入 题库参考。

        题库依赖 AI：仅当 _provider 非空时才查询（无 AI 时查了也无用）。
        题库查询失败/无答案均不影响主流程，返回不含 题库参考 的 extra。
        """
        extra = dict(self._op_extra)
        if self._question_bank is not None:
            hint = self._query_question_bank(question, choices)
            if hint:
                extra["题库参考"] = hint
        return extra

    def _query_question_bank(self, question: QuestionContent, choices: list[dict[str, Any]]) -> str:
        """查询题库，返回格式化提示文本（失败/无答案返回空串）。

        绝不中断做题：所有异常均捕获后返回空串。
        """
        assert self._question_bank is not None
        try:
            title = extract_text(question.content)
            options = "\n".join(extract_text(c["content"]) for c in choices)
            qtype = map_question_type(question.question_type)
            result = self._question_bank.query(title, options, qtype)
            if result is None:
                return ""
            return result.format_hint()
        except Exception as e:
            logger.error(f"题库查询失败: {e}")
            return ""

    # --- 进度条 ---

    def _update_progress(self, source: str) -> None:
        """更新控制台底部进度条"""
        current = self._progress_current
        total = self._progress_total

        # 来源统计
        cache_count = self._progress_sources.count("cached")
        ai_count = sum(1 for s in self._progress_sources if s == "AI generated")
        random_count = sum(1 for s in self._progress_sources if s == "random")

        # 来源标签
        source_parts: list[str] = []
        if cache_count:
            source_parts.append(styled(f"cache:{cache_count}", _C.CYAN))
        if ai_count:
            source_parts.append(styled(f"AI:{ai_count}", _C.BRIGHT_MAGENTA))
        if random_count:
            source_parts.append(styled(f"random:{random_count}", _C.YELLOW))
        source_str = " ".join(source_parts) if source_parts else ""

        # 当前题来源
        if source == "cached":
            cur_tag = styled("[cache]", _C.CYAN)
        elif source == "AI generated":
            cur_tag = styled("[AI]", _C.BRIGHT_MAGENTA)
        elif source == "random":
            cur_tag = styled("[random]", _C.YELLOW)
        elif source == "error":
            cur_tag = styled("[error]", _C.RED)
        else:
            cur_tag = styled(f"{total}题", _C.DIM)

        bar = progress_bar(current, total)
        line = f"  {bar} {cur_tag} {source_str}"
        with contextlib.suppress(Exception):
            self._reporter.progress(line)

    # --- 心跳 ---

    def _heartbeat(self, interval: int = 10) -> None:
        """同步心跳（daemon 线程），定期更新用时

        默认实现：循环调用 _heartbeat_once，间隔 interval 秒。
        子类可重写 _heartbeat_once 实现具体心跳逻辑。
        """
        while not self._stopped:
            try:
                self._heartbeat_once(interval)
            except Exception as e:
                logger.error(f"心跳失败: {e}")
            time.sleep(interval)

    def _heartbeat_once(self, interval: int = 10) -> None:
        """单次心跳（默认实现：调用 updateUserUsedTime）

        子类可重写以自定义心跳逻辑。
        """
        url = f"{self._exam_base_url}/gateway/t/v1/exam/user/updateUserUsedTime"
        data = {
            "examTestId": self._exam_test_id,
            "examPaperId": self._exam_paper_id,
            "heartbeatTime": interval,
        }
        self._api_query(url, data)

    # --- 结果检查 ---

    def _check_results(self, sheets: list[QuestionSheet]) -> tuple[int, int]:
        """检查结果并更新缓存，返回 (正确数, 总题数)"""
        correct_count = 0
        total_count = len(sheets)

        for sheet in sheets:
            question = self._get_question_content(sheet.question_id, sheet.version)
            if question is None:
                continue

            # 检查用户是否答对
            user_answers = question.user_answer_vos
            is_correct = bool(user_answers) and user_answers[0].is_correct == 1
            if is_correct:
                correct_count += 1

            # 获取正确选项（用于缓存更新）
            correct_opts = [opt for opt in question.option_vos if opt.is_correct == 1]

            # 更新缓存（question / answer_content 用 extract_text 清洗 HTML）
            if question.question_type == 3:
                # 填空题：多个空用 / 合并存储
                if correct_opts:
                    cleaned = [extract_text(opt.content) for opt in correct_opts]
                    answer_str = "/".join(cleaned)
                    answer_content_str = "\n".join(cleaned)
                elif user_answers:
                    cleaned = [extract_text(a.answer) for a in user_answers if a.is_correct == 1]
                    answer_str = "/".join(cleaned)
                    answer_content_str = "\n".join(cleaned)
                else:
                    answer_str = ""
                    answer_content_str = ""
            else:
                answer_str = "#@#".join(str(opt.id) for opt in correct_opts)
                answer_content_str = "\n".join(extract_text(opt.content) for opt in correct_opts)

            self._set_cached_answer(
                sheet.question_id,
                {
                    "question": extract_text(question.content),
                    "answer": answer_str,
                    "answer_content": answer_content_str,
                    "questionDict": question.model_dump(),
                },
            )

        self._save_cache()
        return correct_count, total_count

    # --- 缓存持久化（通过 AiExamCache）---

    def _load_cache(self) -> None:
        """加载当前 exam 的缓存到内存（通过 AiExamCache 读取 SQLite）

        仅加载当前 exam，跨 exam 查询通过 search_in_course 按需触发，
        不再预加载课程下所有 exam（去掉 _all_answer_cache 内存合并）。
        """
        self._answer_cache = {}
        exam_entries = self._cache.load_exam(self._course_id, self._exam_test_id)
        for key, value in exam_entries.items():
            if isinstance(value, dict):
                self._answer_cache[key] = value

    def _save_cache(self) -> None:
        """保存当前 exam 内存缓存到 SQLite（通过 AiExamCache.put）

        course_name 从 _op_extra["courseName"] 取，传入 cache.put 以持久化。
        UPSERT 的 CASE WHEN 逻辑会在已有空值时回填，不会覆盖已有非空值。
        """
        course_name = str(self._op_extra.get("courseName", "")) if self._op_extra else ""
        logger.debug(
            f"保存缓存: course_id={self._course_id}, exam_id={self._exam_test_id}, "
            f"course_name={course_name!r}, 条目数={len(self._answer_cache)}"
        )
        for question_id_str, entry in self._answer_cache.items():
            try:
                question_id = int(question_id_str)
                self._cache.put(
                    self._course_id,
                    self._exam_test_id,
                    question_id,
                    entry,
                    course_name=course_name,
                )
            except (ValueError, TypeError) as e:
                logger.error(f"保存缓存失败 question_id={question_id_str}: {e}")
