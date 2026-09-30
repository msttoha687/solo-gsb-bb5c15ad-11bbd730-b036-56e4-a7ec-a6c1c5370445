"""FastAPI application: templates, task creation, revision and result query."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.concurrency import asynccontextmanager
from pydantic import ValidationError

from . import database, scoring
from .review import solve_review_plan
from .schemas import (
    OVERRIDABLE_FIELDS,
    ArbitrationIn,
    AuditEventOut,
    ExamTaskIn,
    PaperScoreSheetOut,
    QuotaConfirmIn,
    QuotaConfirmOut,
    QuotaLeaseOut,
    QuotaOut,
    QuotaReclaimOut,
    QuotaReservationOut,
    QuotaSetIn,
    ReviewPlanIn,
    ReviewPlanOut,
    ReviseTaskIn,
    ScoreBatchBlockerOut,
    ScoreBatchOut,
    ScoreBatchPublishIn,
    ScoreEntryIn,
    SeatPlanIn,
    SeatPlanOut,
    TaskAuditOut,
    TaskCreated,
    TaskQuotaOut,
    TaskResult,
    TaskStatus,
    TemplateConfig,
    TemplateIn,
    TemplateOut,
    TemplateTaskCreated,
    TemplateTaskIn,
)
from .seat_plan import solve_seat_plan
from .worker import submit


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    yield


app = FastAPI(
    title="智能组卷 API",
    description=(
        "面向教研组的题库快照约束组卷服务（FastAPI + SQLite + OR-Tools CP-SAT），"
        "支持 2~5 份平行卷联合组卷、1~5 个差异化候选卷组，"
        "可版本化、创建后不可变的组卷模板，"
        "按题号扣减的题目曝光配额及可过期预留租约（lease），"
        "面向已完成且可行任务的盲审分配，"
        "以及面向已完成且含 2~5 份平行卷的可行任务的考场卷别排布"
        "，以及面向已完成可行任务首个候选卷组的双评录分与仲裁"
        "，以及面向已完成可行任务首个候选卷组的成绩批次发布"
        "（单事务快照一次性冻结全批逐题有效分与总分）"
    ),
    version="1.11.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 题目曝光配额：按题号设置/查询可用次数
# --------------------------------------------------------------------------- #
def _quota_out(row: sqlite3.Row) -> QuotaOut:
    return QuotaOut(
        question_id=json.loads(row["question_id"]),
        quota=row["quota"],
        updated_at=row["updated_at"],
    )


@app.put("/api/quotas", response_model=List[QuotaOut])
def set_quotas(payload: QuotaSetIn) -> List[QuotaOut]:
    """按题号设置曝光配额的可用次数（绝对值，存在即覆盖）。

    题号类型（数字/字符串）需与题库快照中的 id 一致；未设置过配额的题
    在启用配额的任务中按余额 0 处理。
    """
    database.set_quotas([(item.question_id, item.quota) for item in payload.quotas])
    keys = {json.dumps(item.question_id, ensure_ascii=False) for item in payload.quotas}
    return [_quota_out(r) for r in database.list_quotas() if r["question_id"] in keys]


@app.get("/api/quotas", response_model=List[QuotaOut])
def list_quotas() -> List[QuotaOut]:
    """查询全部题号的当前剩余可用次数。"""
    return [_quota_out(r) for r in database.list_quotas()]


@app.post(
    "/api/quotas/reclaim-expired",
    response_model=QuotaReclaimOut,
)
def reclaim_expired_quotas() -> QuotaReclaimOut:
    """回收所有**已过期且未确认**的预留租约：原子释放并标记过期。

    在单个 SQLite 事务（进程内互斥锁 + BEGIN IMMEDIATE）中选出全部
    status=reserved 且 expires_at 已过的租约，逐个按状态+到期条件抢占式置为
    reclaimed，并按 quota_reservations 逐题把 reserved 次数返还到 quotas。
    条件 UPDATE 的行计数是唯一裁决：已确认（confirmed）或已回收（reclaimed）
    的租约不会再命中，因此**重复调用、与确认接口并发都幂等，同一配额绝不释放
    两次**。无到期预留时返回 reclaimed_count=0。
    """
    return QuotaReclaimOut(**database.reclaim_expired_leases())


@app.post("/api/tasks/{task_id}/quota/confirm", response_model=QuotaConfirmOut)
def confirm_task_quota(task_id: int, payload: QuotaConfirmIn) -> QuotaConfirmOut:
    """任务完成后凭 reservation_token 在租约到期前主动确认预留。

    - 任务不存在或无租约返回 404（非租约任务无预留可确认）；
    - 令牌错误返回 403；
    - 预留尚未生效（任务未结算）返回 422（code=awaiting）；任务未产生预留
      （无解/失败/配额不足，租约作废）返回 422（code=void）；
    - 租约已过期或已被回收返回 422（code=expired）——**过期后确认绝不释放或
      重新扣减配额**，过期预留只能由回收接口返还；
    - 到期前确认成功返回 code=confirmed；**重复确认幂等**，返回
      code=already_confirmed 与当前租约状态。
    """
    if database.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    code, lease = database.confirm_quota_reservation(
        task_id, payload.reservation_token
    )
    if code == "no_lease":
        raise HTTPException(status_code=404, detail="该任务没有可确认的配额预留租约")
    if code == "bad_token":
        raise HTTPException(status_code=403, detail="reservation_token 无效")
    if code in ("awaiting", "void", "expired"):
        raise HTTPException(
            status_code=422,
            detail={"message": "预留当前不可确认", "code": code, "lease": lease},
        )
    return QuotaConfirmOut(task_id=task_id, code=code, lease=QuotaLeaseOut(**lease))


@app.post(
    "/api/tasks",
    response_model=TaskCreated,
    status_code=201,
    response_model_exclude_none=True,
)
def create_task(payload: ExamTaskIn) -> TaskCreated:
    """提交组卷任务：题库快照 + 总分/题型/知识点/难度/互斥题组约束。"""
    req = payload.model_dump()
    task_id, reservation_token = database.create_task(
        payload.name,
        json.dumps(req, ensure_ascii=False),
        payload.seed,
        lease_seconds=payload.lease_seconds,
    )
    submit(task_id)
    return TaskCreated(
        task_id=task_id,
        status="pending",
        lease_seconds=payload.lease_seconds,
        reservation_token=reservation_token,
    )


# --------------------------------------------------------------------------- #
# 组卷模板：可版本化、创建后不可变的题库无关约束配置
# --------------------------------------------------------------------------- #
def _template_out(row: sqlite3.Row) -> TemplateOut:
    return TemplateOut(
        template_id=row["id"],
        version=row["version"],
        name=row["name"],
        root_id=row["root_id"],
        base_template_id=row["base_id"],
        config=TemplateConfig(**json.loads(row["config"])),
        created_at=row["created_at"],
    )


@app.post("/api/templates", response_model=TemplateOut, status_code=201)
def create_template(payload: TemplateIn) -> TemplateOut:
    """创建组卷模板（题库无关的约束配置，创建后不可修改）。

    携带 base_template_id 时在该模板的版本谱系上创建新版本（version 递增），
    否则创建 version=1 的全新模板；模板不提供任何修改/删除接口。
    """
    base_row = None
    if payload.base_template_id is not None:
        base_row = database.get_template(payload.base_template_id)
        if base_row is None:
            raise HTTPException(status_code=404, detail="基准模板不存在")
    config_json = json.dumps(payload.config.model_dump(), ensure_ascii=False)
    template_id, _ = database.create_template(payload.name, config_json, base_row)
    return _template_out(database.get_template(template_id))


@app.get("/api/templates/{template_id}", response_model=TemplateOut)
def get_template(template_id: int) -> TemplateOut:
    """查询模板的冻结配置与版本信息。"""
    row = database.get_template(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    return _template_out(row)


def _validation_detail(exc: ValidationError) -> List[Dict[str, Any]]:
    """将 Pydantic 校验错误转换为可 JSON 序列化的 422 明细。"""
    return [
        {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
        for e in exc.errors()
    ]


@app.post(
    "/api/templates/{template_id}/tasks",
    response_model=TemplateTaskCreated,
    status_code=201,
    response_model_exclude_none=True,
)
def create_task_from_template(
    template_id: int, payload: TemplateTaskIn
) -> TemplateTaskCreated:
    """以模板为约束蓝本实例化组卷任务（异步）。

    模板配置与 overrides 逐字段合并（overrides 优先，仅限白名单字段），
    合并结果连同新的题库快照一起按 ExamTaskIn 全量校验；实际生效配置与
    template_id/template_version 一并冻结进任务记录，求解流程与 /api/tasks 一致。
    """
    row = database.get_template(template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模板不存在")

    # 白名单之外的任何字段（未知字段、questions/seed/name 等）一律拒绝
    unknown = sorted(set(payload.overrides) - OVERRIDABLE_FIELDS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"overrides 包含不允许覆盖的字段: {unknown}；"
                f"可覆盖字段（白名单）: {sorted(OVERRIDABLE_FIELDS)}"
            ),
        )

    config = json.loads(row["config"])
    merged = {**config, **payload.overrides}
    name = payload.name if payload.name is not None else row["name"]
    try:
        effective = ExamTaskIn(
            name=name,
            questions=payload.questions,
            seed=payload.seed,
            use_quota=payload.use_quota,
            lease_seconds=payload.lease_seconds,
            **merged,
        )
    except ValidationError as exc:
        # 非法合并：覆盖值自身非法，或与模板其余约束组合后非法
        raise HTTPException(
            status_code=422, detail=_validation_detail(exc)
        ) from exc

    # 冻结实际生效配置与模板溯源信息；kind=template 仅供识别，求解同普通任务
    frozen = effective.model_dump()
    frozen["kind"] = "template"
    frozen["template_id"] = row["id"]
    frozen["template_version"] = row["version"]
    task_id, reservation_token = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        payload.seed,
        lease_seconds=payload.lease_seconds,
    )
    submit(task_id)
    return TemplateTaskCreated(
        task_id=task_id,
        status="pending",
        template_id=row["id"],
        template_version=row["version"],
        lease_seconds=payload.lease_seconds,
        reservation_token=reservation_token,
    )


def _source_paper_selection(
    source_req: Dict[str, Any], source_result: Dict[str, Any]
) -> List[List[Any]]:
    """从源任务结果中提取逐卷选中题号（单卷 -> 单元素列表）。

    多候选源任务以排名第 1（择优最优）的候选为改卷基准。
    """
    paper_count = int(source_req.get("paper_count") or 1)
    alts = source_result.get("alternatives")
    if alts:
        top = min(alts, key=lambda a: a.get("rank", 1))
        if paper_count == 1:
            return [list(top.get("selected_ids") or [])]
        return [list(p.get("selected_ids") or []) for p in top.get("papers") or []]
    if paper_count == 1:
        return [list(source_result.get("selected_ids") or [])]
    return [
        list(p.get("selected_ids") or [])
        for p in source_result.get("papers") or []
    ]


@app.post("/api/tasks/{task_id}/revise", response_model=TaskCreated, status_code=201)
def revise_task(task_id: int, payload: ReviseTaskIn) -> TaskCreated:
    """基于已完成且可行的源任务创建定向改卷任务（异步）：按卷指定必须保留/移除的题号。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="源任务尚未完成，无法改卷")

    source_req = json.loads(row["request"])
    # 改卷以最近一次组卷/改卷的原始请求与源卷选择为准（支持对改卷结果再次改卷）
    if source_req.get("kind") == "revise":
        source_req = source_req["source_request"]
    if not row["result"]:
        raise HTTPException(status_code=422, detail="源任务尚无结果，无法改卷")
    source_result = json.loads(row["result"])
    if source_result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="源任务结果不可行，无法改卷")

    paper_count = int(source_req.get("paper_count") or 1)
    id_set = {q["id"] for q in source_req["questions"]}

    # 卷序号不可重复；不在 [0, paper_count) 内或题号不存在均返回 422
    seen_papers = set()
    for e in payload.edits:
        if e.paper in seen_papers:
            raise HTTPException(status_code=422, detail=f"第 {e.paper} 卷的编辑条件重复指定")
        seen_papers.add(e.paper)
        if e.paper >= paper_count:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"卷序号 {e.paper} 不存在：源任务共 {paper_count} 份卷"
                    f"（卷序号 0~{paper_count - 1}）"
                ),
            )
        missing = [qid for qid in (e.retain_ids + e.remove_ids) if qid not in id_set]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"第 {e.paper} 卷引用了题库中不存在的题号: {missing}",
            )

    # 冻结源请求与逐卷人工编辑条件；源任务本身保持不变
    frozen = {
        "kind": "revise",
        "source_task_id": task_id,
        "source_request": source_req,
        "source_selection": _source_paper_selection(source_req, source_result),
        "edits": [e.model_dump() for e in payload.edits],
    }
    seed = int(source_req.get("seed") or 42)
    name = payload.name if payload.name is not None else row["name"]
    new_task_id, _ = database.create_task(
        name,
        json.dumps(frozen, ensure_ascii=False),
        seed,
    )
    submit(new_task_id)
    return TaskCreated(task_id=new_task_id, status="pending")


# --------------------------------------------------------------------------- #
# 盲审分配：为已完成且可行的组卷任务联合分配审阅人（同步求解，不写库）
# --------------------------------------------------------------------------- #
@app.post(
    "/api/tasks/{task_id}/review-plan",
    response_model=ReviewPlanOut,
    response_model_exclude_none=True,
)
def create_review_plan(task_id: int, payload: ReviewPlanIn) -> ReviewPlanOut:
    """为已完成且结果可行的组卷任务生成盲审分配方案（同步返回，不产生新任务）。

    在**同一个 CP-SAT 联合模型**中把审阅人分配到源任务的全部卷：每卷恰好
    `reviewers_per_paper` 名审阅人（同卷不重复）、审阅人回避其不可审卷号、
    每人跨卷合计不超过其容量。依次最小化：最大个人负载 → 各人负载相对
    平均值的绝对偏差总和 → 按卷号展平的审阅人编号序列（结果可复现）。
    无解时返回 `infeasible` 及各卷可用人力与总容量缺口，不返回部分方案。
    """
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="任务尚未完成，无法生成盲审分配方案")
    if not row["result"]:
        raise HTTPException(status_code=422, detail="任务尚无结果，无法生成盲审分配方案")
    result = json.loads(row["result"])
    if result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="任务结果不可行，无法生成盲审分配方案")

    # 卷数取自冻结的源请求（改卷任务溯源到原始组卷请求）
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        req = req["source_request"]
    paper_count = int(req.get("paper_count") or 1)

    # 不可审卷号越界校验（卷序号从 0 起，上界为源任务的卷数）
    for r in payload.reviewers:
        bad = [p for p in r.excluded_papers if p >= paper_count]
        if bad:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"审阅人 {r.reviewer_id!r} 的不可审卷号 {bad} 越界："
                    f"共 {paper_count} 份卷（卷序号 0~{paper_count - 1}）"
                ),
            )

    plan = solve_review_plan(
        [r.model_dump() for r in payload.reviewers],
        payload.reviewers_per_paper,
        paper_count,
        seed=int(row["seed"]),
    )
    return ReviewPlanOut(task_id=task_id, **plan)


# --------------------------------------------------------------------------- #
# 考场卷别排布：为已完成且含 2~5 份平行卷的可行任务排布考场座位（同步求解，不写库）
# --------------------------------------------------------------------------- #
@app.post(
    "/api/tasks/{task_id}/seat-plan",
    response_model=SeatPlanOut,
    response_model_exclude_none=True,
)
def create_seat_plan(task_id: int, payload: SeatPlanIn) -> SeatPlanOut:
    """为已完成、结果可行且含 2~5 份平行卷的组卷任务生成考场卷别排布。

    输入不超过 6×6 的考场行列数、不可用座位及按卷号给出的各卷印量（印量
    总和须等于可用座位数，否则 422）。求解在**同一个 CP-SAT 模型**中把卷别
    分配到每个可用座位：每座恰放 1 份卷、每卷恰放满印量；按上下左右相邻
    统计同卷别接触边数（不同卷别或邻接空位不计），在严格满足印量的前提下
    使接触边数最少，同最优值按**行优先、卷号升序**唯一固定排布。同步返回、
    不产生新任务、不写库。

    - 任务不存在返回 404；任务未完成 / 无结果 / 结果不可行返回 422；
    - 源任务不是 2~5 份平行卷（单卷任务）返回 422；
    - 印量条数与源任务卷数不一致返回 422。
    """
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    if row["status"] != "done":
        raise HTTPException(status_code=422, detail="任务尚未完成，无法生成考场卷别排布")
    if not row["result"]:
        raise HTTPException(status_code=422, detail="任务尚无结果，无法生成考场卷别排布")
    result = json.loads(row["result"])
    if result.get("status") != "feasible":
        raise HTTPException(status_code=422, detail="任务结果不可行，无法生成考场卷别排布")

    # 卷数取自冻结的源请求（改卷任务溯源到原始组卷请求）
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        req = req["source_request"]
    paper_count = int(req.get("paper_count") or 1)
    if not 2 <= paper_count <= 5:
        raise HTTPException(
            status_code=422,
            detail=(
                f"考场卷别排布仅支持含 2~5 份平行卷的任务：源任务共 {paper_count} 份卷"
            ),
        )
    if len(payload.quantities) != paper_count:
        raise HTTPException(
            status_code=422,
            detail=(
                f"印量条数 {len(payload.quantities)} 与源任务卷数 {paper_count} 不一致："
                f"需按卷号 0~{paper_count - 1} 逐卷给出印量"
            ),
        )

    unavailable = [(s.row, s.col) for s in payload.unavailable_seats]
    plan = solve_seat_plan(
        rows=payload.rows,
        cols=payload.cols,
        unavailable=unavailable,
        quantities=list(payload.quantities),
        seed=int(row["seed"]),
    )
    return SeatPlanOut(task_id=task_id, **plan)


@app.get(
    "/api/tasks/{task_id}",
    response_model=TaskStatus,
    response_model_exclude_none=True,
)
def get_task(task_id: int) -> TaskStatus:
    """查询任务状态与组卷结果（可行卷或最小冲突约束集）。"""
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    result: Optional[TaskResult] = None
    if row["result"]:
        result = TaskResult(**json.loads(row["result"]))

    source_task_id: Optional[int] = None
    req = json.loads(row["request"])
    if req.get("kind") == "revise":
        source_task_id = req.get("source_task_id")

    # 模板实例化任务：回显冻结的模板溯源信息（普通任务不返回这两个字段）
    template_id: Optional[int] = None
    template_version: Optional[int] = None
    if req.get("kind") == "template":
        template_id = req.get("template_id")
        template_version = req.get("template_version")

    # 启用配额的任务：回显本次预留及剩余次数（未声明 use_quota 的任务不返回该字段）
    quota_info: Optional[TaskQuotaOut] = None
    if req.get("use_quota"):
        quota_status: Optional[str] = None
        if row["status"] == "done" and result is not None:
            if result.status == "quota_exceeded":
                quota_status = "exceeded"
            elif result.status == "feasible":
                quota_status = "reserved"
        lease_info: Optional[QuotaLeaseOut] = None
        if req.get("lease_seconds") is not None:
            lease_row = database.get_quota_lease(task_id)
            # 正常必有租约行；即便行被离线删除也只表现为无 lease 块，不影响任务查询
            if lease_row is not None:
                lease_info = QuotaLeaseOut(**database.lease_out(lease_row))
        quota_info = TaskQuotaOut(
            status=quota_status,
            reservations=[
                QuotaReservationOut(
                    question_id=json.loads(r["question_id"]),
                    reserved=r["reserved"],
                    remaining=r["remaining"],
                )
                for r in database.get_quota_reservations(task_id)
            ],
            lease=lease_info,
        )

    return TaskStatus(
        task_id=row["id"],
        status=row["status"],
        name=row["name"],
        seed=row["seed"],
        created_at=row["created_at"],
        source_task_id=source_task_id,
        template_id=template_id,
        template_version=template_version,
        quota=quota_info,
        result=result,
    )


# --------------------------------------------------------------------------- #
# 防篡改审计链：重放哈希链并与业务表交叉校验，返回事件链与首个断点
# --------------------------------------------------------------------------- #
@app.get("/api/tasks/{task_id}/audit", response_model=TaskAuditOut)
def get_task_audit(task_id: int) -> TaskAuditOut:
    """查询任务的防篡改审计事件链及校验结论。

    返回按序号排列的全部事件（created / started / finished / quota_settled）
    与逐条重放 SHA-256 哈希链、交叉比对任务/配额业务表后的校验结论：

    - ``valid=true``：序号从 1 连续、前驱哈希逐环咬合、事件内容摘要全部重算
      一致，且请求/结果摘要、配额预留与业务表实际内容相符；
    - ``valid=false``：``break_point`` 给出**首个**断点的序号、类别与
      期望/实际值（不伪报通过），``events`` 仍返回链上全部记录以便定位。

    事件哈希口径：对 ``{task_id, seq, type, created_at, payload, prev_hash}``
    的规范化 JSON（UTF-8、键升序、无空白、不转义非 ASCII）计算 SHA-256；
    首事件前驱为 64 个 0。
    """
    # 任务行、事件链、预留明细在单一只读事务快照中读取并校验，避免跨连接
    # 瞬时不一致造成误报；快照内查无任务即 404
    snapshot = database.get_audit_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    _, event_rows, break_point = snapshot
    events = [
        AuditEventOut(
            seq=r["seq"],
            type=r["event_type"],
            created_at=r["created_at"],
            payload=json.loads(r["payload"]),
            prev_hash=r["prev_hash"],
            hash=r["hash"],
        )
        for r in event_rows
    ]
    return TaskAuditOut(
        task_id=task_id,
        valid=break_point is None,
        verified_at=datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        events=events,
        break_point=break_point,
        last_hash=event_rows[-1]["hash"] if event_rows else None,
    )


# --------------------------------------------------------------------------- #
# 录分：已完成可行任务首个候选卷组上的初评/复评/仲裁（乐观版本，持久化）
# --------------------------------------------------------------------------- #
def _scorable_paper(task_id: int, paper: int):
    """校验任务可录分并返回 (逐卷题号列表, 题号->题目分值)。

    任务不存在 404；任务未完成 / 无结果 / 结果不可行 422；卷号越界 422。
    """
    row = database.get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    try:
        paper_count, papers, score_map = scoring.load_task_papers(row)
    except scoring.TaskNotScorableError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if paper < 0 or paper >= paper_count:
        raise HTTPException(
            status_code=422,
            detail=(
                f"卷号 {paper} 不存在：该任务首个候选卷组共 {paper_count} 份卷"
                f"（卷号 0~{paper_count - 1}）"
            ),
        )
    return papers, score_map


def _decode_id(raw: str, what: str):
    """解码路径中的考生标识/题号；非 int/str（布尔、浮点、数组等）一律 422。"""
    value = scoring.decode_id_key(raw)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{what} 必须是整数或字符串（收到 {type(value).__name__}）；"
                f"整数与字符串形式严格区分"
            ),
        )
    return value


def _resolve_question(question_raw: str, selected_ids: List[Any], score_map: Dict[str, int]):
    """题号必须属于该卷（首个候选卷组冻结题目），返回 (题号, 存储键, 题目分值)。"""
    qid = _decode_id(question_raw, "题号")
    if qid not in selected_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                f"题号 {qid!r} 不在该卷题目中；该卷仅允许题目 {selected_ids}"
            ),
        )
    return qid, scoring.encode_id_key(qid), score_map[qid]


def _build_sheet(
    task_id: int, paper: int, examinee_id: Any, selected_ids: List[Any]
) -> PaperScoreSheetOut:
    rows = database.get_paper_score_rows(
        task_id, paper, scoring.encode_id_key(examinee_id)
    )
    data = scoring.build_score_sheet(task_id, paper, examinee_id, selected_ids, rows)
    return PaperScoreSheetOut(**data)


@app.put(
    "/api/tasks/{task_id}/papers/{paper}/examinees/{examinee_raw}/questions/{question_raw}/scores",
    response_model=PaperScoreSheetOut,
    response_model_exclude_none=True,
)
def put_question_score(
    task_id: int,
    paper: int,
    examinee_raw: str,
    question_raw: str,
    payload: ScoreEntryIn,
) -> PaperScoreSheetOut:
    """录入/修改某考生某题的**初评或复评**分（持久化）。

    路径：任务号、卷号（从 0 起）、考生标识、题号（均须属于首个候选卷组）；
    体：``initial_score`` 或 ``review_score``（恰好一个，0 至题目分值的整数）
    及改分时必填的 ``version``。

    - 该题首次录分或**补评**（填补缺失的初评/复评空槽）：无需 version，
      每题版本从 0 起；
    - 已有该轮分数再提交即**改分**：必须携带当前 version，成功后版本 +1，
      且**旧仲裁立即撤销**；同分重复提交幂等（版本不变）；
    - 改分缺 version 返回 422（code=version_required）；版本不符返回
      409（code=version_conflict），**不写入任何内容**。

    返回与 GET 同一结构的最新成绩单（同快照逐题两评分、有效分、版本、
    待处理原因；全卷每题均有有效分时附总分）。
    """
    papers, score_map = _scorable_paper(task_id, paper)
    selected_ids = papers[paper]
    examinee_id = _decode_id(examinee_raw, "考生标识")
    qid, question_key, max_score = _resolve_question(
        question_raw, selected_ids, score_map
    )
    if payload.score > max_score:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{'初评' if payload.stage == 'initial' else '复评'}分 {payload.score}"
                f" 超出第 {qid!r} 题分值上限 {max_score}（允许 0~{max_score} 的整数）"
            ),
        )

    code, _ = database.put_paper_score(
        task_id,
        paper,
        scoring.encode_id_key(examinee_id),
        question_key,
        payload.stage,
        payload.score,
        payload.version,
    )
    if code == "version_missing":
        raise HTTPException(
            status_code=422,
            detail={
                "message": "该轮评分已存在，提交即改分，必须携带当前 version",
                "code": "version_required",
            },
        )
    if code == "version_conflict":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "版本不符：题目已被其他请求改动，未写入",
                "code": "version_conflict",
            },
        )
    return _build_sheet(task_id, paper, examinee_id, selected_ids)


@app.post(
    "/api/tasks/{task_id}/papers/{paper}/examinees/{examinee_raw}/questions/{question_raw}/arbitration",
    response_model=PaperScoreSheetOut,
    response_model_exclude_none=True,
)
def arbitrate_question_score(
    task_id: int,
    paper: int,
    examinee_raw: str,
    question_raw: str,
    payload: ArbitrationIn,
) -> PaperScoreSheetOut:
    """录入/修改某考生某题的**仲裁分**（持久化）。

    仲裁**仅限初评、复评齐备且相差超过 1 分**：缺轮须先补评（422，
    code=awaiting_review）；分差不超过 1 分时两评均值即为有效分，不允许仲裁
    （422，code=arbitration_not_needed）。仲裁分须为 0 至题目分值的整数；
    必须携带当前 version，成功后版本 +1，同分重复提交幂等。任何对初评/复评的
    改分都会立即撤销旧仲裁，撤销后若分差仍超过 1 分可凭新版本重新仲裁。
    """
    papers, score_map = _scorable_paper(task_id, paper)
    selected_ids = papers[paper]
    examinee_id = _decode_id(examinee_raw, "考生标识")
    qid, question_key, max_score = _resolve_question(
        question_raw, selected_ids, score_map
    )
    if payload.score > max_score:
        raise HTTPException(
            status_code=422,
            detail=f"仲裁分 {payload.score} 超出第 {qid!r} 题分值上限 {max_score}"
            f"（允许 0~{max_score} 的整数）",
        )

    # 仲裁前置条件以当前快照的两评分为准（缺轮 / 分差 <=1 一律拒绝）
    existing = database.get_paper_score_rows(
        task_id, paper, scoring.encode_id_key(examinee_id)
    )
    state = scoring.question_state(
        next((r for r in existing if r["question_id"] == question_key), None)
    )
    if state["initial_score"] is None or state["review_score"] is None:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "初评、复评尚未齐备，无法仲裁，请先补评",
                "code": "awaiting_review",
            },
        )
    if abs(int(state["initial_score"]) - int(state["review_score"])) <= 1:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "两评分相差不超过 1 分，取均值即为有效分，无需仲裁",
                "code": "arbitration_not_needed",
            },
        )

    code, _ = database.arbitrate_paper_score(
        task_id,
        paper,
        scoring.encode_id_key(examinee_id),
        question_key,
        payload.score,
        payload.version,
    )
    if code == "no_score_row":
        # 理论上前置快照应已命中行；并发被删时保守按缺评处理
        raise HTTPException(
            status_code=422,
            detail={
                "message": "初评、复评尚未齐备，无法仲裁，请先补评",
                "code": "awaiting_review",
            },
        )
    if code == "version_missing":
        raise HTTPException(
            status_code=422,
            detail={"message": "仲裁必须携带当前 version", "code": "version_required"},
        )
    if code == "version_conflict":
        raise HTTPException(
            status_code=409,
            detail={
                "message": "版本不符：题目已被其他请求改动，未写入",
                "code": "version_conflict",
            },
        )
    return _build_sheet(task_id, paper, examinee_id, selected_ids)


@app.get(
    "/api/tasks/{task_id}/papers/{paper}/examinees/{examinee_raw}/scores",
    response_model=PaperScoreSheetOut,
    response_model_exclude_none=True,
)
def get_paper_scores(task_id: int, paper: int, examinee_raw: str) -> PaperScoreSheetOut:
    """在**同一快照**返回某考生某卷的逐题两评分、有效分、版本与待处理原因。

    题目按首个候选卷组的 ``selected_ids`` 逐题返回（未录分题同样列出，两评分
    为 null、版本 0、原因为 no_scores）。有效分：两评分齐备且分差不超过 1 取
    均值，分差超过 1 且有仲裁取仲裁分，其余为 null 并给 pending_reason；
    **全卷每题均有有效分时**才返回 ``total_score``，否则该字段省略。

    路径中的考生标识以 JSON 编码区分类型：整数 ``42`` 写作 ``42``，字符串
    ``X7`` 写作 ``%22X7%22``（即 ``"X7"`` 的 URL 编码）——整数 42 与字符串
    ``"42"`` 是两个不同的考生。
    """
    papers, _ = _scorable_paper(task_id, paper)
    examinee_id = _decode_id(examinee_raw, "考生标识")
    return _build_sheet(task_id, paper, examinee_id, papers[paper])


# --------------------------------------------------------------------------- #
# 成绩批次发布：单事务快照冻结全批逐题有效分与总分（只追加、历史批次不可变）
# --------------------------------------------------------------------------- #
def _decode_batch_id(batch_raw: str):
    """解码路径中的批次标识；非 int/str 拒绝，空字符串批次标识同样拒绝。"""
    batch_id = _decode_id(batch_raw, "批次标识")
    if isinstance(batch_id, str) and not batch_id:
        raise HTTPException(status_code=422, detail="批次标识不能为空字符串")
    return batch_id


@app.put(
    "/api/tasks/{task_id}/papers/{paper}/score-batches/{batch_raw}",
    response_model=ScoreBatchOut,
    responses={
        201: {"description": "批次首次冻结成功"},
        200: {"description": "相同批次、相同名单的幂等重试，返回原冻结快照"},
        409: {"description": "批次已存在但请求名单不一致（batch_conflict），不写入"},
    },
)
def publish_score_batch(
    task_id: int,
    paper: int,
    batch_raw: str,
    payload: ScoreBatchPublishIn,
    response: Response,
) -> ScoreBatchOut:
    """把首个候选卷组某卷上**一批考生**的当前有效成绩一次性冻结发布。

    路径给出任务号、卷号（从 0 起）与批次标识；请求体给出考生名单
    （非空、无重复，整数与字符串标识严格区分）。批次标识在同一任务及卷内
    唯一，路径编码口径与考生标识相同：整数批次 ``7`` 写作 ``7``，字符串批次
    ``"B1"`` 写作 ``%22B1%22``——整数 7 与字符串 "7" 是两个不同批次。

    服务端在**单个写事务（BEGIN IMMEDIATE）的同一数据库快照**内读取名单内
    每名考生该卷的全部录分行并逐题套用有效分口径：

    - 每名考生的该卷**每题都有有效分**：整批一次性冻结（逐题有效分 + 总分，
      总分口径与 GET 成绩单一致），返回 201；冻结只追加，此后改分或仲裁**不
      改变**本批次，新批次才读取最新成绩；
    - 任一人缺评或待仲裁：返回 **422**（code=scores_pending），``blockers``
      逐人给出第一道未决题（question_id/reason/message），**整批回滚不发布**；
    - **相同批次重试**（名单逐位置、逐标识完全一致）：返回 **200** 与原冻结
      快照（published_at 等均为首次冻结值）；同批次但名单不一致：返回 **409**
      （code=batch_conflict，附既有名单），不写入任何内容；
    - 空名单 / 重复考生 / 非整数非字符串标识返回 422；任务不存在 404；任务
      未完成或结果不可行、卷号越界返回 422。
    """
    papers, _ = _scorable_paper(task_id, paper)
    batch_id = _decode_batch_id(batch_raw)

    code, data = database.publish_score_batch(
        task_id,
        paper,
        scoring.encode_id_key(batch_id),
        [scoring.encode_id_key(x) for x in payload.examinee_ids],
        papers[paper],
    )
    if code == "created":
        response.status_code = 201
    elif code == "exists":
        response.status_code = 200
    elif code == "conflict":
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    "批次已存在且本次请求名单与冻结名单不一致；"
                    "历史批次不可修改，请改用新批次标识"
                ),
                "code": "batch_conflict",
                "existing_examinee_ids": data,
            },
        )
    else:  # pending
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "名单内存在缺评或待仲裁的考生，整批未发布；"
                    "补齐后可凭相同批次标识重试"
                ),
                "code": "scores_pending",
                "blockers": [
                    ScoreBatchBlockerOut(**b).model_dump() for b in data
                ],
            },
        )
    return ScoreBatchOut(**data)


@app.get(
    "/api/tasks/{task_id}/papers/{paper}/score-batches/{batch_raw}",
    response_model=ScoreBatchOut,
)
def get_score_batch(task_id: int, paper: int, batch_raw: str) -> ScoreBatchOut:
    """按批次查询已发布的冻结成绩（逐考生逐题有效分与总分、冻结时刻、名单）。

    返回的恒为发布时刻的快照：发布后的初评/复评改分与仲裁均不影响结果。
    任务不存在返回 404；任务未完成/结果不可行/卷号越界返回 422；任务、卷号
    有效但批次不存在返回 **404**（批次标识整数/字符串按编码严格区分）。
    """
    papers, _ = _scorable_paper(task_id, paper)
    batch_id = _decode_batch_id(batch_raw)
    data = database.get_score_batch(
        task_id, paper, scoring.encode_id_key(batch_id)
    )
    if data is None:
        raise HTTPException(status_code=404, detail="该任务该卷下批次不存在")
    return ScoreBatchOut(**data)
