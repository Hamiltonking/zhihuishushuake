#!/usr/bin/env python3
"""AI 课程作业续跑器

`zhs homework` 启动时会走 CAS 票据校验（verify_session），CASTGC 过期后即使 API
会话仍然可用也会直接退出。本脚本跳过该校验：只用本地 cookies 恢复会话，直接进入
作业流程，适合长时间挂机跑掌握度。

用法：
    .venv/bin/python tools/resume_homework.py 2094350321505583104 254981 --only-zero
    .venv/bin/python tools/resume_homework.py 2094350321505583104 254981 --max-tries 2
    .venv/bin/python tools/resume_homework.py 2094350321505583104 254981 --only-zero --start 全面从严治党
    .venv/bin/python tools/resume_homework.py 2094350321505583104 254981 --only-zero --start-index 120
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

from zhs.cli.bootstrap import init_question_bank, setup_logger
from zhs.cli.services.homework_service import run_ai_homework
from zhs.config import ConfigManager
from zhs.exceptions import ZhsError
from zhs.login import LoginManager
from zhs.session import ZhsSession

ROOT = Path(__file__).resolve().parent.parent


def enable_only_zero() -> None:
    """只做掌握度仍为 0 的知识点：做过一次（哪怕只拿到 67 分）就不再重做

    默认逻辑是 mastery > ai_homework_threshold(90) 才跳过，会把 79/83 分反复重刷。
    """
    from zhs.ai.course import AiCourseManager
    from zhs.ai.models import ExamInfo
    from zhs.config import HomeworkConfig

    def _should_do_homework(
        self: AiCourseManager,
        exam: ExamInfo,
        tried: int,
        no_homework: bool,
        homework_config: HomeworkConfig,
    ) -> bool:
        if no_homework:
            return False
        return exam.mastery_score == 0

    AiCourseManager._should_do_homework = _should_do_homework  # type: ignore[method-assign]


def enable_max_tries(max_tries: int) -> None:
    """每个知识点最多尝试 max_tries 次，之后无论分数多少都跳过

    默认逻辑不看尝试次数，只要 mastery <= ai_homework_threshold 就无限重刷。
    """
    from zhs.ai.course import AiCourseManager
    from zhs.ai.models import ExamInfo
    from zhs.config import HomeworkConfig

    def _should_do_homework(
        self: AiCourseManager,
        exam: ExamInfo,
        tried: int,
        no_homework: bool,
        homework_config: HomeworkConfig,
    ) -> bool:
        if no_homework:
            return False
        if exam.mastery_score > homework_config.ai_homework_threshold:
            return False
        return tried < max_tries

    AiCourseManager._should_do_homework = _should_do_homework  # type: ignore[method-assign]


def enable_start_at(start_text: str = "", start_index: int = 0) -> None:
    """从指定位置开始跑，之前的知识点直接跳过（不查询、不联网）

    默认每次运行都会从课程第一个知识点开始扫，扫到要做的位置要花几分钟。
    指定起始点后，解析课程结构时就直接截断，前面的知识点完全不处理。

    Args:
        start_text: 知识点名或主题名里包含的关键字（先匹配到谁就从谁开始）
        start_index: 从第几个知识点开始（1 开始计数），start_text 非空时忽略
    """
    from zhs.ai.course import AiCourseManager
    from zhs.ai.models import AiCourseInfo, KnowledgeTarget

    original = AiCourseManager._iter_targets

    def _iter_targets(self: AiCourseManager, course_info: AiCourseInfo) -> Iterator[KnowledgeTarget]:
        started = False
        position = 0
        for target in original(self, course_info):
            position += 1
            if started:
                yield target
                continue
            if start_text and (start_text in target.knowledge.knowledge_name or start_text in target.theme_name):
                started = True
            elif start_index and position >= start_index:
                started = True
            if started:
                setattr(self, "_target_offset", position - 1)
            if started:
                print(f"从第 {position} 个知识点开始：{target.knowledge.knowledge_name}")
                yield target
        if not started:
            print(f"警告：没找到起始位置（start_text={start_text!r}, start_index={start_index}），本次不处理任何知识点", file=sys.stderr)

    AiCourseManager._iter_targets = _iter_targets  # type: ignore[method-assign]


def main() -> int:
    positional: list[str] = []
    max_tries = 0
    only_zero = False
    start_text = ""
    start_index = 0
    i = 0
    while i < len(sys.argv[1:]):
        arg = sys.argv[1 + i]
        if arg == "--max-tries":
            i += 1
            max_tries = int(sys.argv[1 + i])
        elif arg == "--start":
            i += 1
            start_text = sys.argv[1 + i]
        elif arg == "--start-index":
            i += 1
            start_index = int(sys.argv[1 + i])
        elif arg == "--only-zero":
            only_zero = True
        elif not arg.startswith("--"):
            positional.append(arg)
        i += 1

    if len(positional) < 2:
        print(
            "用法: resume_homework.py <courseId> <classId> "
            "[--only-zero | --max-tries N] [--start 关键字 | --start-index N]",
            file=sys.stderr,
        )
        return 2
    course_id, class_id = int(positional[0]), int(positional[1])

    config = ConfigManager().load()
    setup_logger(config, debug=False, console_log=True)
    if max_tries > 0:
        enable_max_tries(max_tries)
        print(f"模式: 每个知识点最多 {max_tries} 次尝试（阈值 {config.homework.ai_homework_threshold}）")
    elif only_zero:
        enable_only_zero()
        print("模式: 只做掌握度为 0 的知识点")

    if start_text or start_index:
        enable_start_at(start_text=start_text, start_index=start_index)

    session = ZhsSession(config)
    if not LoginManager(session, config).try_restore_cookies(ROOT / "personal" / "cookies.json"):
        print("Cookie 恢复失败，请先运行 zhs login", file=sys.stderr)
        return 1

    try:
        run_ai_homework(session, config, course_id, class_id)
    except ZhsError as exc:
        print(f"作业中断: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
