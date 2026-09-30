"""Background worker: claim-based execution with crash recovery.

任务在创建事务中落库后，执行权不依赖任何常驻线程。worker 执行前必须先经
``database.claim_task`` **原子领取**：pending 先到先得，running 仅在心跳陈旧
（原执行进程已退出）时可被抢占，done 永不重算。条件 UPDATE 是跨进程、重复
启动、与新提交并发时的唯一裁决，同一任务同时只有一个领取者。

本模块在进程内维护：

- 单线程串行执行槽（组卷为 CPU 密集型任务，串行保证结果确定性与 SQLite 写入安全）；
- ``_inflight`` 集合：已被本进程排队/执行的任务不重复排队（进程内去重，
  跨进程去重由 claim 栅栏保证）；
- 心跳守护线程：周期性为持有的 running 任务续租，存活执行期间不会被误接管；
- 接管扫描守护线程：周期性领取全部 pending 与心跳陈旧的 running 任务——
  服务启动时立即扫描一次，因此**重启后自动接管**悬置任务；之后按
  ``EXAM_TASK_SWEEP_SECONDS``（默认 10 秒）周期重扫，心跳在
  ``EXAM_TASK_STALE_SECONDS``（默认 30 秒）后才判陈旧，故存活 worker 的任务
  在其崩溃后最迟约一个陈旧窗口 + 一个扫描周期被接管。

完成结算统一走 ``database.settle_task``：提交结果前再次校验持有令牌，未持有
则整体放弃（连配额扣减一并回滚），因此**任务只有一个最终结果、启用配额的
任务只结算一次**。恢复执行本身抛出异常时，异常被捕获并落为可查询的
``status=error`` 结果（配额任务同时作废租约、补 void 结算事件）。
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import threading
import traceback

from . import database
from .solver import quota_requirements, solve_exam, solve_revise

# 组卷为 CPU 密集型任务，单线程串行执行即可保证结果确定性与 SQLite 写入安全。
_semaphore = threading.Semaphore(1)

#: 心跳续租间隔（秒）：远小于陈旧阈值，保证存活执行期间任务不被误接管。
HEARTBEAT_SECONDS = 5

#: 接管扫描周期（秒）：启动时额外立即扫描一次。
SWEEP_SECONDS = int(os.environ.get("EXAM_TASK_SWEEP_SECONDS", "10"))

#: 本进程 worker 的不透明持有令牌（领取/心跳/结算全程以此身份出现）。
WORKER_ID = f"pid-{os.getpid()}-{secrets.token_hex(8)}"

#: 已被本进程排队或正在执行的任务 id（进程内去重；受 _queue_lock 保护）。
_inflight: set[int] = set()
_queue_lock = threading.Lock()
_task_queue: "queue.Queue[int]" = queue.Queue()

_started = False
_start_lock = threading.Lock()


def _execute(task_id: int) -> None:
    """领取 -> 求解 -> 结算单个任务；任何异常都落为可查询结果，不向上抛。"""
    try:
        row = database.claim_task(task_id, WORKER_ID)
        if row is None:
            # 已被其它 worker/实例领取或已完成：本进程不重复执行
            return
        try:
            req = json.loads(row["request"])
        except (ValueError, TypeError):
            # 冻结请求本身损坏：无法求解，仍收敛为可查询的错误结果并作废租约
            database.settle_task_error_fallback(task_id, WORKER_ID)
            return

        try:
            if req.get("kind") == "revise":
                result = solve_revise(req)
            else:
                # 普通任务与模板实例化任务（kind=template）求解路径完全一致
                result = solve_exam(req)
        except Exception as exc:  # noqa: BLE001 - 恢复执行异常也落库，任务可查询
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

        # 统一结算入口：内部再校验持有令牌，未持有时整体放弃（不产生第二份结果）
        requirements = None
        if req.get("use_quota") and result.get("status") == "feasible":
            requirements = quota_requirements(result)
        database.settle_task(task_id, WORKER_ID, result, requirements)
    finally:
        with _queue_lock:
            _inflight.discard(task_id)


def _worker_loop() -> None:
    """串行消费任务队列；单个任务异常不影响后续任务。"""
    while True:
        task_id = _task_queue.get()
        with _semaphore:
            try:
                _execute(task_id)
            except Exception:  # noqa: BLE001 - 守护线程绝不让异常静默杀死消费者
                traceback.print_exc()
                with _queue_lock:
                    _inflight.discard(task_id)
        _task_queue.task_done()


def _enqueue(task_id: int) -> bool:
    """把任务放入本进程执行队列；已在队列/执行中则跳过。返回是否新入队。"""
    with _queue_lock:
        if task_id in _inflight:
            return False
        _inflight.add(task_id)
    _task_queue.put(task_id)
    return True


def submit(task_id: int) -> None:
    """新提交任务的入口：不立即领取，仅入队（领取发生在实际执行前）。

    领取延后到执行时刻，可让“排队中”的任务同样由 claim 栅栏统一裁决，
    避免与恢复扫描/其它实例产生双重执行。
    """
    _start()
    _enqueue(task_id)


def recover_once() -> dict:
    """执行一轮接管扫描：领取全部 pending 与心跳陈旧的 running 任务并入队。

    返回扫描报告：``claimed`` 为本次新入队接管的任务 id；``already_running``
    为心跳仍新鲜、本进程不介入的 running 任务；``inflight`` 为已在本进程
    队列/执行中因而跳过的任务。``done`` 任务不在任何列表中——已完成任务与
    冻结成绩批次绝不重算。
    """
    _start()
    inflight_snapshot = _snapshot_inflight()
    claimed: list[int] = []
    for row in database.list_recoverable_tasks():
        tid = int(row["id"])
        if _enqueue(tid):
            claimed.append(tid)
    # 全部 running 任务 = 本轮领取 + 本进程已在执行 + 其它存活 worker 执行中
    claimed_or_inflight = set(claimed) | inflight_snapshot
    already_running = [
        int(r["id"])
        for r in database.list_active_running_tasks()
        if int(r["id"]) not in claimed_or_inflight
    ]
    return {
        "claimed": claimed,
        "already_running": already_running,
        "inflight": sorted(inflight_snapshot),
        "claimed_count": len(claimed),
    }


def _snapshot_inflight() -> set[int]:
    with _queue_lock:
        return set(_inflight)


def _heartbeat_loop() -> None:
    """定期为本进程持有的 running 任务续租心跳。"""
    ev = threading.Event()
    while not ev.wait(HEARTBEAT_SECONDS):
        try:
            database.heartbeat(WORKER_ID)
        except Exception:  # noqa: BLE001 - 续租失败等待下一轮，不杀死守护线程
            traceback.print_exc()


def _sweep_loop() -> None:
    """启动后立即接管一轮，之后周期性重扫 pending / 陈旧 running 任务。"""
    recover_once()
    ev = threading.Event()
    while not ev.wait(SWEEP_SECONDS):
        try:
            report = recover_once()
        except Exception:  # noqa: BLE001 - 扫描异常等待下一轮，不杀死守护线程
            traceback.print_exc()
            continue
        if report["claimed"]:
            print(f"[worker] recovered unfinished tasks: {report['claimed']}", flush=True)


def _start() -> None:
    """惰性启动执行线程、心跳线程与接管扫描线程（进程内仅一次）。"""
    global _started
    with _start_lock:
        if _started:
            return
        threading.Thread(target=_worker_loop, name="exam-worker", daemon=True).start()
        threading.Thread(
            target=_heartbeat_loop, name="exam-worker-heartbeat", daemon=True
        ).start()
        threading.Thread(
            target=_sweep_loop, name="exam-worker-recovery", daemon=True
        ).start()
        _started = True


def startup_recover() -> dict:
    """服务启动钩子：初始化守护线程并立即接管一轮悬置任务（同步返回首轮报告）。"""
    _start()
    return recover_once()
