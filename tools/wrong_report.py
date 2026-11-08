#!/usr/bin/env python3
"""题目报告：把缓存里的题目连同正确答案导出来（默认只看错题）

数据来源：`personal/cache/questions_bank.db` 的 `ai_questions` 表。
服务端在判分时会回传每题的正确答案（`option_vos[*].is_correct`）和你提交的答案
（`user_answer_vos`），这里据此生成错题清单。

用法：
    .venv/bin/python tools/wrong_report.py                 # 只看错题（默认习概课程）
    .venv/bin/python tools/wrong_report.py --all           # 所有题目，含做对的
    .venv/bin/python tools/wrong_report.py --course 123456
    .venv/bin/python tools/wrong_report.py --out /tmp/x.md
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

DEFAULT_COURSE = 2094350321505583104
TYPE_NAMES = {1: "单选", 2: "多选", 3: "填空", 14: "判断"}


def _option_map(qd: dict) -> dict[str, str]:
    """选项 id -> 文本"""
    return {str(opt.get("id")): str(opt.get("content", "")).strip() for opt in qd.get("option_vos") or []}


def _render_answer(qd: dict, question_type: int, submitted: str, correct_content: str) -> tuple[str, str]:
    """返回 (你答的, 正确答案) 的可读文本"""
    options = _option_map(qd)

    if question_type == 3:
        mine = submitted.replace("#@#", " / ")
        return mine or "(空)", correct_content.replace("\n", " / ") or "(未记录)"

    mine_ids = [x for x in submitted.split("#@#") if x]
    mine = " / ".join(options.get(i, i) for i in mine_ids) or "(空)"
    correct = " / ".join(text for i, text in options.items() if _is_correct(qd, i)) or correct_content
    return mine, correct


def _is_correct(qd: dict, option_id: str) -> bool:
    for opt in qd.get("option_vos") or []:
        if str(opt.get("id")) == option_id and opt.get("is_correct") == 1:
            return True
    return False


def collect(course_id: int, db_path: Path, include_correct: bool = False) -> list[dict[str, object]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT exam_id, question_id, question, answer_content, question_dict, last_updated "
        "FROM ai_questions WHERE course_id=? ORDER BY last_updated DESC",
        (course_id,),
    ).fetchall()

    wrong: list[dict[str, object]] = []
    for row in rows:
        qd = json.loads(row["question_dict"])
        user_answers = qd.get("user_answer_vos") or []
        is_correct = bool(user_answers) and user_answers[0].get("is_correct") == 1
        if is_correct and not include_correct:
            continue
        if not user_answers:
            continue
        qtype = int(qd.get("question_type") or 0)
        mine, correct = _render_answer(qd, qtype, str(user_answers[0].get("answer", "")), row["answer_content"] or "")
        wrong.append(
            {
                "question_id": row["question_id"],
                "question": (row["question"] or "").strip(),
                "mine": mine,
                "correct": correct,
                "type": TYPE_NAMES.get(qtype, f"type{qtype}"),
                "exam_id": row["exam_id"],
                "time": str(row["last_updated"]).replace("T", " "),
                "is_correct": is_correct,
            }
        )
    return wrong


def render_markdown(course_id: int, wrong: list[dict[str, object]], title: str = "错题本") -> str:
    by_type = Counter(str(item["type"]) for item in wrong)
    right = sum(1 for item in wrong if item.get("is_correct"))
    lines = [
        f"# {title}",
        "",
        f"- 课程 ID：`{course_id}`",
        f"- 题目数：**{len(wrong)}**（做对 {right} / 做错 {len(wrong) - right}）",
        f"- 题型分布：{', '.join(f'{k} {v} 道' for k, v in by_type.items()) or '无'}",
        "",
        "| # | 题目 | 你答的 | 正确答案 | 结果 | 题型 | 时间 |",
        "|---|------|--------|----------|------|------|------|",
    ]
    for idx, item in enumerate(wrong, 1):
        question = str(item["question"]).replace("|", "\\|").replace("\n", " ")[:60]
        mine = str(item["mine"]).replace("|", "\\|").replace("\n", " ")[:40]
        correct = str(item["correct"]).replace("|", "\\|").replace("\n", " ")[:40]
        result = "✅" if item.get("is_correct") else "❌"
        lines.append(f"| {idx} | {question} | {mine} | {correct} | {result} | {item['type']} | {item['time']} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    here = Path(__file__).resolve().parent          # tools/
    data_dir = here.parent / "personal"             # 个人数据目录
    parser = argparse.ArgumentParser(description="导出做错的题目与正确答案")
    parser.add_argument("--course", type=int, default=DEFAULT_COURSE, help="课程 ID")
    parser.add_argument("--out", type=Path, default=None, help="Markdown 输出路径（默认写到 personal/）")
    parser.add_argument("--all", action="store_true", help="包含做对的题（默认只导出错题）")
    args = parser.parse_args()

    db_path = data_dir / "cache" / "questions_bank.db"
    if not db_path.exists():
        print(f"缓存库不存在: {db_path}")
        return 1

    wrong = collect(args.course, db_path, include_correct=args.all)
    out_path = args.out or (data_dir / ("questions_all.md" if args.all else "wrong_questions.md"))
    report = render_markdown(args.course, wrong, title="题目汇总" if args.all else "错题本")
    out_path.write_text(report, encoding="utf-8")

    print(f"题目 {len(wrong)} 道，已写入 {out_path}\n")
    for idx, item in enumerate(wrong[:10], 1):
        print(f"{idx}. [{item['type']}] {str(item['question'])[:42]}")
        print(f"   你答: {str(item['mine'])[:46]}")
        print(f"   正确: {str(item['correct'])[:46]}")
    if len(wrong) > 10:
        print(f"... 其余 {len(wrong) - 10} 道见 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
