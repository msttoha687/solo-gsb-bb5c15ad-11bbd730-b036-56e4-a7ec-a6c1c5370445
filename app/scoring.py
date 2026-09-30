"""录分领域逻辑：首个候选卷组解析、有效分/待处理原因、单快照成绩单组装。

教研组只对**已完成且可行**的组卷任务录分，且基准恒为排名第 1（择优最优）的
候选卷组（单候选即结果本身；多候选取 ``alternatives`` 中 ``rank`` 最小者）。
每份卷的题目以该候选冻结的 ``selected_ids`` 为准，题目分值取自任务冻结请求
中的题库快照（改卷任务溯源到其 ``source_request``），因此“仅允许该卷题目、
分数为 0 至题目分值的整数”均可在写库前离线校验。

有效分口径：
  - 初评、复评齐备且 **相差不超过 1 分**：有效分取两者均值（差 0 为整数，
    差 1 为带 0.5 的半分），无需仲裁；
  - 两评分齐备但相差超过 1 分：本身不产生有效分，等待仲裁；仲裁分（范围内
    整数）落定后即作为有效分；
  - 仅一轮评分或缺轮：等待补评，仲裁也不允许。
任何对初评/复评的改分都会立即撤销旧仲裁（见 database.put_paper_score），
因此仲裁分只可能在“最近一次两评分差 >1”时有效，无需额外失效标记。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

#: 待处理原因码 -> 说明。``None`` 表示已有有效分。
PENDING_REASONS: Dict[str, str] = {
    "no_scores": "尚无初评与复评，等待两轮评分",
    "missing_initial": "缺少初评，等待补评",
    "missing_review": "缺少复评，等待补评",
    "awaiting_arbitration": "初评与复评分差超过 1 分，等待仲裁",
}

#: 有效分阈值：|初评 - 复评| 不超过该值时直接取均值。
SCORE_DIFF_LIMIT = 1


class TaskNotScorableError(ValueError):
    """任务不存在、未完成或结果不可行（不可录分）。"""


def _effective_request(row: sqlite3.Row) -> Dict[str, Any]:
    """取录分基准请求：改卷任务溯源到其冻结的原始组卷请求。"""
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        return req["source_request"]
    return req


def first_candidate_papers(
    task: sqlite3.Row,
) -> Tuple[int, List[List[Any]], Dict[str, int]]:
    """解析已完成可行任务的首个候选卷组。

    返回 (卷数, 逐卷选中题号列表, 题号 -> 题目分值)。任务未完成或结果不可行
    （含 infeasible / error / quota_exceeded）时抛 TaskNotScorableError。
    多候选取 rank 最小者；单候选直接读结果本体。
    """
    if task["status"] != "done" or not task["result"]:
        raise TaskNotScorableError("任务尚未完成，无法录分")
    result = json.loads(task["result"])
    if result.get("status") != "feasible":
        raise TaskNotScorableError("任务结果不可行，无法录分")

    req = _effective_request(task)
    score_map = {q["id"]: int(q["score"]) for q in req["questions"]}

    source = result
    alts = result.get("alternatives")
    if alts:
        source = min(alts, key=lambda a: a.get("rank", 1))

    papers = source.get("papers")
    if papers:
        selected = [list(p.get("selected_ids") or []) for p in papers]
    else:
        selected = [list(source.get("selected_ids") or [])]
    return len(selected), selected, score_map


def load_task_papers(task: Optional[sqlite3.Row]) -> Tuple[int, List[List[Any]], Dict[str, int]]:
    """任务存在性包装：不存在直接拒绝，其余委托 first_candidate_papers。"""
    if task is None:
        raise TaskNotScorableError("任务不存在")
    return first_candidate_papers(task)


def question_state(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    """把一行录分记录（或无记录）计算为逐题评分状态。

    返回字段：initial_score / review_score / arbitration_score（均可空）、
    version（无记录为 0）、effective_score（可空）、pending_reason /
    pending_message（有有效分时为 null）。
    """
    initial = review = arbitration = None
    version = 0
    if row is not None:
        initial = row["initial_score"]
        review = row["review_score"]
        arbitration = row["arbitration_score"]
        version = int(row["version"])

    effective: Optional[float] = None
    reason: Optional[str] = None
    if initial is None and review is None:
        reason = "no_scores"
    elif initial is None:
        reason = "missing_initial"
    elif review is None:
        reason = "missing_review"
    elif abs(int(initial) - int(review)) <= SCORE_DIFF_LIMIT:
        # 两评分齐备且分差 <=1：取均值（差 0 为整数、差 1 为 .5）
        avg = (int(initial) + int(review)) / 2
        effective = int(avg) if float(avg).is_integer() else avg
    elif arbitration is not None:
        # 分差 >1 且已有仲裁分（范围内整数）：仲裁分为有效分
        effective = int(arbitration)
    else:
        reason = "awaiting_arbitration"

    return {
        "initial_score": initial,
        "review_score": review,
        "arbitration_score": arbitration,
        "version": version,
        "effective_score": effective,
        "pending_reason": reason,
        "pending_message": PENDING_REASONS[reason] if reason is not None else None,
    }


def build_score_sheet(
    task_id: int,
    paper: int,
    examinee_id: Any,
    selected_ids: List[Any],
    rows: List[sqlite3.Row],
) -> Dict[str, Any]:
    """在同一批快照读取结果上组装某考生某卷的逐题成绩单。

    题目顺序严格按首个候选卷组的 ``selected_ids``；未录分题同样返回（空槽 +
    待处理原因）。仅当每题都有有效分时 total_score 才有值。
    """
    by_question = {json.loads(r["question_id"]): r for r in rows}
    questions: List[Dict[str, Any]] = []
    total: Optional[float] = 0
    for qid in selected_ids:
        state = question_state(by_question.get(qid))
        questions.append({"question_id": qid, **state})
        if state["effective_score"] is None:
            total = None
        elif total is not None:
            total += state["effective_score"]
    if total is not None and float(total).is_integer():
        total = int(total)
    return {
        "task_id": task_id,
        "paper": paper,
        "examinee_id": examinee_id,
        "questions": questions,
        "total_score": total,
    }


def decode_id_key(raw: str) -> Any:
    """把路径/查询中的标识（考生标识或题号）解码为 int 或 str。

    先按 JSON 解码（``"42"`` -> 42、``"\"X7\""`` -> "X7"）；非法 JSON 退化为
    原始字符串。布尔与浮点等非 int/str 结果由调用方拒绝（返回 422）。
    """
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return raw
    return value


def encode_id_key(value: Any) -> str:
    """标识的存储键：JSON 编码，区分整数 1 与字符串 "1"（考生/题号同一口径）。"""
    return json.dumps(value, ensure_ascii=False)
