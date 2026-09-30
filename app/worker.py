"""Background worker: runs composition tasks in a daemon thread pool.

任务执行权通过 :func:`database.claim_task` 的**条件状态抢占**获得
（pending/running -> running，BEGIN IMMEDIATE + 行计数裁决）：

- 新提交与服务启动后的**崩溃恢复**走完全相同的执行路径——冻结请求、种子与
  原任务号都已在创建时落库，恢复只是重新抢占并求解，普通、模板实例化与
  定向改卷任务一视同仁（改卷任务的冻结请求内自带源请求与编辑条件）；
- 恢复与新提交并发、或服务被重复启动时，同一任务只有一个执行者能抢占成功，
  落库同样带 running 条件，因此一个任务**只有一个最终结果**，启用配额的
  任务**只结算一次**；
- pending 任务（从未开始）由 claim_task 在抢占事务内补记 started 事件；
  悬置的 running 任务（服务退出时正在求解）已有 started，恢复不重复记录；
- 已 done 的任务（含已冻结成绩批次所属任务）不会被恢复选中，**绝不重算**；
- 求解或落库环节的任何异常都转成可查询的 ``error`` 结果（同事务落库并记
  finished 事件），任务不会再悬置。
"""
from __future__ import annotations

import json
import logging
import threading
import traceback

from . import database
from .solver import quota_requirements, solve_exam, solve_revise

logger = logging.getLogger("uvicorn.error")

# 组卷为 CPU 密集型任务，单线程串行执行即可保证结果确定性与 SQLite 写入安全。
_semaphore = threading.Semaphore(1)

#: 本进程内已排队/执行中的任务，避免同进程重复排队（跨进程/重复启动的
#: 去重最终由 claim_task 的条件抢占与落库行计数保证）。
_inflight: set[int] = set()
_inflight_lock = threading.Lock()


def _persist_result(task_id: int, req, result: dict) -> None:
    """把求解结果按配额口径原子落库；任一失败都转成可查询的 error 结果。"""
    try:
        use_quota = bool(isinstance(req, dict) and req.get("use_quota"))
        has_lease = bool(isinstance(req, dict) and req.get("lease_seconds"))
        if use_quota and result.get("status") == "feasible":
            settled = database.finish_task_with_quota(
                task_id, result, quota_requirements(result)
            )
            if settled is None:
                # 任务已被其他执行者完成：本次结果整体丢弃，配额未被扣减
                logger.warning("task %s 已由其他执行者完成，放弃本次结算", task_id)
            return
        ok = database.finish_task(
            task_id,
            json.dumps(result, ensure_ascii=False),
            void_lease=has_lease,
        )
        if not ok:
            logger.warning("task %s 已由其他执行者落库，放弃本次结果", task_id)
    except Exception as exc:  # noqa: BLE001 - 落库失败也要形成可查询的终态
        logger.exception("task %s 结果落库失败", task_id)
        fallback = {
            "status": "error",
            "error": f"结果落库失败: {type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
        try:
            if isinstance(req, dict) and req.get("kind") == "revise":
                fallback["source_task_id"] = req["source_task_id"]
        except Exception:  # noqa: BLE001 - 溯源补充失败不应掩盖原异常
            pass
        # 仍带 running 条件：若任务已被他处落库则不再覆盖；配额租约随失败作废
        database.finish_task(
            task_id,
            json.dumps(fallback, ensure_ascii=False),
            void_lease=bool(isinstance(req, dict) and req.get("lease_seconds")),
        )


def _execute(task_id: int) -> None:
    """抢占执行权 -> 读取冻结请求 -> 求解 -> 落库（全程串行于单工作许可内）。"""
    try:
        claimed = database.claim_task(task_id)
        if claimed == "stale":
            # 不存在、已完成或已被其他执行者接管：直接退出，不产生第二个结果
            return
        row = database.get_task(task_id)
        if row is None:
            return
        try:
            req = json.loads(row["request"])
        except Exception as exc:  # noqa: BLE001 - 冻结请求损坏也要给出可查询终态
            logger.exception("task %s 冻结请求无法解析", task_id)
            _persist_result(
                task_id,
                None,
                {
                    "status": "error",
                    "error": f"冻结请求无法解析: {type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                },
            )
            return

        try:
            if req.get("kind") == "revise":
                result = solve_revise(req)
            else:
                result = solve_exam(req)
        except Exception as exc:  # noqa: BLE001 - 任何异常都落库，任务可查询
            logger.exception("task %s 求解异常", task_id)
            result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
            # 改卷任务异常时也保留源任务溯源信息
            try:
                if req.get("kind") == "revise":
                    result["source_task_id"] = req["source_task_id"]
            except Exception:  # noqa: BLE001 - 溯源补充失败不应掩盖原异常
                pass
        # 题目曝光配额：仅启用配额且组卷可行的任务参与扣减；
        # 无解/失败任务不消耗任何配额（改卷任务不声明 use_quota，天然跳过）。
        # 带租约的配额任务在无解/失败时把租约同事务作废（void_lease=True），
        # 成功/配额不足的租约流转在 finish_task_with_quota 内与扣减同事务完成。
        _persist_result(task_id, req, result)
    finally:
        with _inflight_lock:
            _inflight.discard(task_id)


def submit(task_id: int) -> bool:
    """把任务排入后台执行队列；本进程已排队时返回 False（不重复排队）。

    最终的跨线程/跨进程去重由 claim_task 的条件抢占保证，这里只避免同进程
    内无意义的重复排队。
    """
    with _inflight_lock:
        if task_id in _inflight:
            return False
        _inflight.add(task_id)
    threading.Thread(
        target=_guarded_run, args=(task_id,), daemon=True, name=f"task-{task_id}"
    ).start()
    return True


def _guarded_run(task_id: int) -> None:
    """单工作许可串行执行；线程内任何漏网异常都不得影响后续恢复任务。"""
    with _semaphore:
        try:
            _execute(task_id)
        except Exception:  # noqa: BLE001 - 守护线程兜底，绝不让异常静默杀死任务
            logger.exception("task %s 执行发生未捕获异常", task_id)


def recover_pending() -> list[int]:
    """服务启动后自动接管所有悬置任务，返回本次实际重新排队的任务号列表。

    选取状态为 pending（已落库但未开始）与 running（服务退出时正在求解、
    结果未落库）的任务，按 ID 升序逐个重新排队；done 任务（结果已落库，
    含已冻结成绩批次所属任务）不重算。重复调用幂等：已完成或已在执行的
    任务不会被再次接管。
    """
    task_ids = database.list_unfinished_tasks()
    recovered: list[int] = []
    for task_id in task_ids:
        if submit(task_id):
            recovered.append(task_id)
    if recovered:
        logger.info("启动恢复：接管 %d 个悬置任务 %s", len(recovered), recovered)
    return recovered
