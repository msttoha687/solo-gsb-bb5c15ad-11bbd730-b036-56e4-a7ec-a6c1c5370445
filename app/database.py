"""SQLite persistence for composition tasks.

任务状态机：pending -> running -> done（结果写入 result 列）。
后台线程与请求线程各自建立连接，配合进程内互斥锁串行化写操作。

组卷模板（templates 表）：创建后不可变——只提供插入与查询，没有任何更新
接口；修改约束配置只能通过创建新版本（同一 root_id 谱系下 version 递增）。

题目曝光配额（quotas / quota_reservations / quota_leases 表）：按题号维护
可用次数；启用配额的任务在可行组卷完成时，于**单个 SQLite 事务**（BEGIN IMMEDIATE）
中串行化校验并原子扣减——任一题余额不足则整组不扣减，任务结果改写为
quota_exceeded 并附缺额明细；扣减成功时逐题写入预留记录（含扣后剩余快照），
供任务查询接口回显“本次预留及剩余次数”。

预留租约（quota_leases 表）：带 lease_seconds 创建的配额任务在创建时即落一条
awaiting 租约并下发一次性 reservation_token；配额成功扣减的同一事务把租约激活为
reserved 并冻结 expires_at（结算时刻 + lease_seconds）。持 token 在到期前可调用
确认接口把租约原子置为 confirmed（配额永久扣减，重复确认幂等）；过期未确认的预留
由回收接口在**单个 SQLite 事务**中按条件更新抢占式回收（reserved -> reclaimed）
并逐题返还配额，条件 UPDATE 保证确认与回收、回收与回收并发互斥，同一配额绝不会
释放两次。任务最终未产生预留（无解/失败/配额不足）时租约在同事务置为 void。

防篡改审计链（task_audit_events 表）：任务创建（created）、开始（started）、
完成或失败（finished）及配额结算（quota_settled）均在**同一任务状态写入所在
的既有 SQLite 事务内**追加事件，与业务改动原子提交。事件含序号 seq（同任务
从 1 连续递增）、UTC 时间、类型与最小业务载荷；事件哈希为规范化 JSON 载荷
（含 task_id/seq/type/created_at/payload/prev_hash，不含 hash 本身）的
SHA-256，首事件前驱为 64 个 0（创世哈希）。校验时逐条重算哈希、核对序号与
前驱，并与 tasks/quota_reservations 表中的实际内容摘要交叉比对，发现首个
断点即停止，绝不伪报通过。

录分（paper_scores 表）：面向**已完成且可行**任务的首个候选卷组，按
(卷号, 考生标识, 题号) 持久化初评、复评与仲裁分。考生标识与题号均以 JSON
编码存储（整数 1 与字符串 "1" 互不冲突）。每题版本从 0 起：首次录分与补评
填空槽不动版本；改分与仲裁为乐观并发，必须携带当前版本，条件 UPDATE 命中
后版本 +1，版本不符整句不命中、绝不写入；任何改分在同语句把仲裁分置空
（旧仲裁立即撤销）。读取在单一只读事务快照内完成，查询所见严格同一时刻。

成绩批次发布（score_batches / score_batch_items 表）：在**单个写事务**
（进程内互斥锁 + BEGIN IMMEDIATE）的同一数据库快照内读取批次名单内每名
考生该卷的全部录分行并逐题套用有效分口径——仅当每人每题均有有效分时才
一次性插入批次头（同任务+卷内批次标识唯一）与逐考生逐题有效分/总分的
冻结明细并原子提交；任一人缺评或待仲裁则回滚整事务，整批不发布。同批次
重试名单一致返回原冻结快照，不一致报冲突且不写入；冻结只追加、永不更新，
发布后的改分/仲裁不改变历史批次，新批次读取最新成绩。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

DB_PATH = os.environ.get("EXAM_DB_PATH", "/data/exam.db")

_lock = threading.Lock()

#: 审计链创世前驱哈希：每个任务首事件的 prev_hash 固定为 64 个 0。
GENESIS_HASH = "0" * 64


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with _lock, get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                status      TEXT NOT NULL DEFAULT 'pending',
                request     TEXT NOT NULL,
                result      TEXT,
                seed        INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS templates (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT,
                config      TEXT NOT NULL,
                version     INTEGER NOT NULL,
                root_id     INTEGER,
                base_id     INTEGER,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 题目曝光配额：question_id 存 JSON 编码的题号（整数 1 与字符串 "1" 互不冲突）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quotas (
                question_id TEXT PRIMARY KEY,
                quota       INTEGER NOT NULL CHECK (quota >= 0),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 每次成功扣减的预留明细（remaining 为本次扣减后的剩余快照）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_reservations (
                task_id     INTEGER NOT NULL,
                question_id TEXT NOT NULL,
                reserved    INTEGER NOT NULL,
                remaining   INTEGER NOT NULL,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, question_id)
            )
            """
        )
        # 可过期的预留租约：每个设置了 lease_seconds 的配额任务一行。
        # status 状态机：awaiting（已建约、任务未结算，expires_at 为空）
        #   -> reserved（配额已扣减、租约生效，冻结 expires_at）
        #   -> confirmed（到期前持 token 主动确认，配额永久扣减）
        #   -> reclaimed（过期后被回收接口原子释放，配额已逐题返还）；
        # 任务未产生预留（无解/失败/配额不足）则 awaiting -> void。
        # 状态流转一律走带状态/到期条件的 UPDATE 抢占，杜绝同一配额释放两次。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quota_leases (
                task_id          INTEGER PRIMARY KEY,
                reservation_token TEXT NOT NULL UNIQUE,
                lease_seconds    INTEGER NOT NULL CHECK (lease_seconds > 0),
                status           TEXT NOT NULL DEFAULT 'awaiting',
                expires_at       TEXT,
                confirmed_at     TEXT,
                released_at      TEXT,
                created_at       TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # 录分：已完成可行任务首个候选卷组上，按 (卷号, 考生, 题) 存初评/复评/仲裁。
        # examinee_id / question_id 均为 JSON 编码（整数 1 与字符串 "1" 互不冲突）。
        # version 从 0 起：首次录分/补评填空槽不动版本；改分与仲裁为乐观并发，
        # 必须携带当前版本，命中后 version+1，版本不符的写入整句不命中、绝不落库。
        # 任何改分在同一条 UPDATE 内把 arbitration_score 置空（旧仲裁立即撤销）。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_scores (
                task_id           INTEGER NOT NULL,
                paper             INTEGER NOT NULL,
                examinee_id       TEXT NOT NULL,
                question_id       TEXT NOT NULL,
                initial_score     INTEGER CHECK (initial_score IS NULL OR initial_score >= 0),
                review_score      INTEGER CHECK (review_score IS NULL OR review_score >= 0),
                arbitration_score INTEGER CHECK (arbitration_score IS NULL OR arbitration_score >= 0),
                version           INTEGER NOT NULL DEFAULT 0,
                updated_at        TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, paper, examinee_id, question_id)
            )
            """
        )
        # 防篡改审计链：同一任务 seq 从 1 连续递增、唯一；prev_hash 链向前一
        # 事件哈希（首事件为 64 个 0）；hash 为事件内容（规范化 JSON）的 SHA-256。
        # payload 为规范化 JSON 文本（最小业务载荷），事件只追加、永不更新/删除。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS task_audit_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id    INTEGER NOT NULL,
                seq        INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload    TEXT NOT NULL,
                prev_hash  TEXT NOT NULL,
                hash       TEXT NOT NULL,
                UNIQUE (task_id, seq)
            )
            """
        )
        # 成绩批次发布：score_batches 为批次头（同任务+卷内 batch_id 唯一），
        # score_batch_items 为逐考生冻结快照。批次只在**单个 BEGIN IMMEDIATE
        # 事务**中产生：该事务同一快照读取名单内每名考生该卷的全部录分行，
        # 逐题套用有效分口径，仅当每人每题均有有效分时才插入批次头与全部明细，
        # 任一人缺评或待仲裁则整事务回滚——绝无“半批发布”。
        # batch_id / examinee_id 均为 JSON 编码（整数 1 与字符串 "1" 互不冲突）。
        # examinees_json 冻结请求名单（顺序敏感，重试一致性按它判定）；
        # items 行的 sheet_json 冻结逐题有效分与总分，发布后录分改动不影响历史批次。
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS score_batches (
                task_id       INTEGER NOT NULL,
                paper         INTEGER NOT NULL,
                batch_id      TEXT NOT NULL,
                examinees_json TEXT NOT NULL,
                published_at  TEXT NOT NULL,
                created_at    TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, paper, batch_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS score_batch_items (
                task_id     INTEGER NOT NULL,
                paper       INTEGER NOT NULL,
                batch_id    TEXT NOT NULL,
                seq         INTEGER NOT NULL,
                examinee_id TEXT NOT NULL,
                sheet_json  TEXT NOT NULL,
                PRIMARY KEY (task_id, paper, batch_id, seq),
                UNIQUE (task_id, paper, batch_id, examinee_id)
            )
            """
        )


def create_task(
    name: Optional[str],
    request_json: str,
    seed: int,
    lease_seconds: Optional[int] = None,
) -> Tuple[int, Optional[str]]:
    """创建任务，返回 (task_id, reservation_token)。

    lease_seconds 为正整数时在**同一事务**内预建 awaiting 租约并生成一次性
    reservation_token；租约此时尚未生效（expires_at 为空），要等任务可行结算
    扣减配额时才激活。未设置租约时 token 返回 None。
    """
    with _lock, get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (name, status, request, seed) VALUES (?, 'pending', ?, ?)",
            (name, request_json, seed),
        )
        task_id = int(cur.lastrowid)
        reservation_token: Optional[str] = None
        if lease_seconds is not None:
            reservation_token = _new_reservation_token()
            conn.execute(
                "INSERT INTO quota_leases"
                " (task_id, reservation_token, lease_seconds, status)"
                " VALUES (?, ?, ?, 'awaiting')",
                (task_id, reservation_token, int(lease_seconds)),
            )
        # created 事件与任务行（及租约行）在同一事务原子提交；载荷内冻结请求摘要，
        # 事后任何对 tasks.request 的篡改都会在交叉校验中暴露
        append_audit_event(
            conn,
            task_id,
            "created",
            {
                "name": name,
                "seed": seed,
                "request_sha256": _sha256_text(request_json),
            },
        )
        return task_id, reservation_token


def _new_reservation_token() -> str:
    """生成不可猜测的一次性预留确认令牌（URL 安全随机串）。"""
    return secrets.token_urlsafe(32)


def claim_task(task_id: int) -> str:
    """原子抢占一个待执行任务的执行权（供新提交与崩溃恢复共用）。

    在**单个事务**（进程内互斥锁 + BEGIN IMMEDIATE）内按状态条件把任务置为
    running，条件 UPDATE 的行计数是唯一裁决：

    - ``claimed_pending``：原状态为 pending，置 running 并在**同一事务**补记
      started 审计事件（pending -> running 与事件原子提交，崩溃也不丢事件）；
    - ``claimed_running``：原状态为 running——典型为服务退出时正在求解、结果
      尚未落库的悬置任务。started 事件在首次开始时已落库，恢复只抢占执行权、
      重新求解，**绝不重复记 started**（异常缺失时才补记且仅补一次）；
    - ``stale``：任务不存在、已 done 或已被其他执行者/实例抢占——调用方必须
      直接退出，从而保证恢复与新提交、重复启动并发时一个任务只有一个最终结果。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] not in ("pending", "running"):
            conn.commit()
            return "stale"
        previous = row["status"]
        cur = conn.execute(
            "UPDATE tasks SET status = 'running' WHERE id = ?"
            " AND status IN ('pending', 'running')",
            (task_id,),
        )
        if cur.rowcount != 1:
            # 并发抢占失败：其他执行者已先一步接管/完成
            conn.commit()
            return "stale"
        has_started = conn.execute(
            "SELECT 1 FROM task_audit_events WHERE task_id = ?"
            " AND event_type = 'started' LIMIT 1",
            (task_id,),
        ).fetchone()
        if not has_started:
            # pending 首次开始；或遗留 running 异常缺失 started（仅补一次，
            # 审计链仍为 created -> started -> finished）
            append_audit_event(conn, task_id, "started", {"status": "running"})
        conn.commit()
        return "claimed_pending" if previous == "pending" else "claimed_running"


def list_unfinished_tasks() -> List[int]:
    """列出所有未完成任务（pending 或服务退出时悬置的 running），按 ID 升序。

    done 任务（结果已落库，含已冻结成绩批次所属任务）一律不在其中，恢复永不重算。
    """
    with get_conn() as conn:
        return [
            int(r["id"])
            for r in conn.execute(
                "SELECT id FROM tasks WHERE status IN ('pending', 'running')"
                " ORDER BY id"
            ).fetchall()
        ]


def finish_task(
    task_id: int,
    result_json: str,
    void_lease: bool = False,
) -> bool:
    """落库任务最终结果。仅当前为 running 的任务可被完成（条件 UPDATE 裁决）。

    返回 True 表示本次调用完成了任务；False 表示任务不存在或已不是 running
    （已完成/已被其他执行者落库）——此时调用方不得再写结果或追加审计事件，
    保证一个任务只有一个最终结果。审计 finished 事件与结果更新、租约作废在
    **同一事务**原子提交。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE tasks SET status = 'done', result = ?"
            " WHERE id = ? AND status = 'running'",
            (result_json, task_id),
        )
        if cur.rowcount != 1:
            conn.commit()
            return False
        # 带租约的配额任务最终未走扣减结算（无解/失败）：租约在同事务作废，
        # 任何持 token 的确认请求都不可能再得到配额
        if void_lease:
            conn.execute(
                "UPDATE quota_leases SET status = 'void' WHERE task_id = ?"
                " AND status = 'awaiting'",
                (task_id,),
            )
        # feasible / infeasible / error 统一记为 finished，成败以 payload.status 区分；
        # 事件与任务结果落库原子提交，载荷冻结结果摘要
        append_audit_event(
            conn,
            task_id,
            "finished",
            {
                "result_status": json.loads(result_json).get("status"),
                "result_sha256": _sha256_text(result_json),
            },
        )
        conn.commit()
        return True


def get_task(task_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


# --------------------------------------------------------------------------- #
# 防篡改审计链：规范化 JSON + SHA-256 哈希链，事件随业务写入在同事务原子追加
# --------------------------------------------------------------------------- #
def _canonical_json(value: Any) -> str:
    """审计规范化 JSON：UTF-8、键升序、无空白、不转义非 ASCII。

    追加事件与校验重算必须走同一序列化口径，任何字符级差异都会使哈希不一致。
    """
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _sha256_text(text: str) -> str:
    """对文本的 UTF-8 字节计算 SHA-256 十六进制摘要。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    """事件时间：UTC ISO 8601（秒级，Z 结尾）。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def append_audit_event(
    conn: sqlite3.Connection, task_id: int, event_type: str, payload: Dict[str, Any]
) -> None:
    """在**调用方已开启的事务/连接**中追加一个审计事件。

    不自建连接、不自开事务：必须与触发它的业务改动（任务创建、状态流转、
    结果落库、配额扣减）在同一个 SQLite 事务中提交或回滚。同任务的 seq 取
    当前最大值 +1（从 1 起），prev_hash 取上一事件哈希（首事件为创世哈希）。
    """
    row = conn.execute(
        "SELECT seq, hash FROM task_audit_events WHERE task_id = ?"
        " ORDER BY seq DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if row is None:
        seq, prev_hash = 1, GENESIS_HASH
    else:
        seq, prev_hash = int(row["seq"]) + 1, row["hash"]

    created_at = _now_iso()
    payload_json = _canonical_json(payload)
    # 哈希体不含 hash 自身；prev_hash 在体内，形成逐事件咬合的哈希链
    body = _canonical_json(
        {
            "task_id": task_id,
            "seq": seq,
            "type": event_type,
            "created_at": created_at,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )
    conn.execute(
        "INSERT INTO task_audit_events"
        " (task_id, seq, event_type, created_at, payload, prev_hash, hash)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, seq, event_type, created_at, payload_json, prev_hash,
         _sha256_text(body)),
    )


def get_audit_events(task_id: int) -> List[sqlite3.Row]:
    """按序号读取某任务的全部审计事件（链上原始记录，只追加不改写）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT task_id, seq, event_type, created_at, payload, prev_hash, hash"
            " FROM task_audit_events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()


def _audit_break(
    at_seq: Optional[int], kind: str, expected: Any = None, actual: Any = None
) -> Dict[str, Any]:
    """构造首个断点描述：定位序号 + 异常类别 + 期望/实际值。

    expected/actual 为 None 时不带入响应（避免把“无”误展示成具体值）。
    """
    detail: Dict[str, Any] = {"at_seq": at_seq, "kind": kind}
    if expected is not None:
        detail["expected"] = expected
    if actual is not None:
        detail["actual"] = actual
    return detail


#: 事件合法生命周期顺序（created 固定首位；settlement 必须在 finished 之后）。
_EVENT_PHASE = {"created": 0, "started": 1, "finished": 2, "quota_settled": 3}


def _verify_audit_chain(
    task: sqlite3.Row,
    events: List[sqlite3.Row],
    reservations: List[sqlite3.Row],
) -> Optional[Dict[str, Any]]:
    """在调用方事务的一致快照上重放校验审计链。

    校验顺序（命中即返回，不伪报通过）：
    1. 空链 / 首事件非 created / 序号连续性（seq 必须从 1 起逐条 +1）；
    2. 前驱哈希：首事件须为创世哈希（64 个 0），其余须等于上一事件哈希；
    3. 内容摘要：按规范化 JSON 重算事件体哈希，必须与存储 hash 一致，
       且 payload 必须是合法的规范化 JSON 对象；
    4. 生命周期：created/started/finished 各至多一次且按 created → started →
       finished → quota_settled 顺序出现，未知事件类型直接判断点；
    5. 与业务表交叉比对：created 事件的请求摘要/名称/种子须与 tasks 行一致，
       finished 事件的结果摘要/状态须与 tasks.result 一致，quota_settled
       事件须与 quota_reservations 实际预留（或 quota_exceeded 缺额）一致；
    6. 状态完备性：running/done 的任务必须存在 started 事件；done 任务必须
       存在 finished 事件；声明 use_quota 的任务必须存在 quota_settled 事件。
    """
    if not events:
        return _audit_break(None, "empty_chain", expected="created as seq 1")

    prev_hash = GENESIS_HASH
    seen_phases: Dict[str, int] = {}
    settlement_payloads: List[Tuple[int, Dict[str, Any]]] = []
    finished_seen = False
    for index, ev in enumerate(events):
        seq = int(ev["seq"])
        expected_seq = index + 1

        # 1. 序号连续
        if seq != expected_seq:
            return _audit_break(
                seq, "seq_gap", expected=expected_seq, actual=seq
            )

        # 首事件必须是 created
        if index == 0 and ev["event_type"] != "created":
            return _audit_break(
                seq,
                "unexpected_first_event",
                expected="created",
                actual=ev["event_type"],
            )

        # 2. 前驱哈希
        if ev["prev_hash"] != prev_hash:
            return _audit_break(
                seq,
                "prev_hash_mismatch",
                expected=prev_hash,
                actual=ev["prev_hash"],
            )

        # 3a. payload 必须可解析为 JSON 对象
        try:
            payload = json.loads(ev["payload"])
        except (ValueError, TypeError):
            return _audit_break(seq, "payload_not_json", actual=ev["payload"])
        if not isinstance(payload, dict):
            return _audit_break(
                seq, "payload_not_object", actual=type(payload).__name__
            )

        # 3b. 事件内容摘要（与追加时完全同口径重算）
        body = _canonical_json(
            {
                "task_id": int(ev["task_id"]),
                "seq": seq,
                "type": ev["event_type"],
                "created_at": ev["created_at"],
                "payload": payload,
                "prev_hash": ev["prev_hash"],
            }
        )
        actual_hash = _sha256_text(body)
        if actual_hash != ev["hash"]:
            return _audit_break(
                seq, "hash_mismatch", expected=ev["hash"], actual=actual_hash
            )

        # 3c. payload 文本必须就是规范化结果（杜绝“语义相同但字节被改动”）
        if _canonical_json(payload) != ev["payload"]:
            return _audit_break(
                seq, "payload_not_canonical",
                expected=_canonical_json(payload), actual=ev["payload"],
            )

        # 4. 生命周期顺序与唯一性
        etype = ev["event_type"]
        phase = _EVENT_PHASE.get(etype)
        if phase is None:
            return _audit_break(seq, "unknown_event_type", actual=etype)
        if etype == "created":
            if index != 0:
                return _audit_break(
                    seq, "duplicate_event",
                    expected="created only at seq 1", actual=etype,
                )
        else:
            if etype in seen_phases:
                return _audit_break(
                    seq, "duplicate_event",
                    expected=f"single {etype} event",
                    actual=f"also at seq {seen_phases[etype]}",
                )
            # started/finished/settlement 之前不得缺少更早阶段的事件
            required_predecessors = {
                "started": ["created"],
                "finished": ["created", "started"],
                "quota_settled": ["created", "started", "finished"],
            }[etype]
            missing = [p for p in required_predecessors if p not in seen_phases]
            if missing:
                return _audit_break(
                    seq, "event_order",
                    expected=f"{etype} after {' -> '.join(required_predecessors)}",
                    actual=f"missing {missing}",
                )
        seen_phases[etype] = seq

        # 5. 与业务表交叉比对
        if etype == "created":
            if payload.get("request_sha256") != _sha256_text(task["request"]):
                return _audit_break(
                    seq, "request_digest_mismatch",
                    expected=payload.get("request_sha256"),
                    actual=_sha256_text(task["request"]),
                )
            if payload.get("seed") != int(task["seed"]):
                return _audit_break(
                    seq, "seed_mismatch",
                    expected=payload.get("seed"), actual=int(task["seed"]),
                )
            if payload.get("name") != task["name"]:
                return _audit_break(
                    seq, "name_mismatch",
                    expected=payload.get("name"), actual=task["name"],
                )
        elif etype == "started":
            if payload.get("status") != "running":
                return _audit_break(
                    seq, "invalid_payload",
                    expected={"status": "running"}, actual=payload,
                )
        elif etype == "finished":
            finished_seen = True
            result_json = task["result"]
            if not result_json:
                return _audit_break(
                    seq, "result_missing",
                    expected="tasks.result populated with finished event",
                )
            if payload.get("result_sha256") != _sha256_text(result_json):
                return _audit_break(
                    seq, "result_digest_mismatch",
                    expected=payload.get("result_sha256"),
                    actual=_sha256_text(result_json),
                )
            try:
                actual_status = json.loads(result_json).get("status")
            except ValueError:
                return _audit_break(seq, "result_not_json", actual=result_json)
            if payload.get("result_status") != actual_status:
                return _audit_break(
                    seq, "result_status_mismatch",
                    expected=payload.get("result_status"), actual=actual_status,
                )
        elif etype == "quota_settled":
            break_detail = _verify_quota_settlement(task, payload, reservations)
            if break_detail is not None:
                return _audit_break(seq, **break_detail)
            settlement_payloads.append((seq, payload))

        prev_hash = ev["hash"]

    # 6. 状态完备性（断点定位到链尾之后，at_seq=null）
    if task["status"] in ("running", "done") and "started" not in seen_phases:
        return _audit_break(
            None, "started_event_missing",
            expected=f"started event for task status {task['status']}",
        )
    if task["status"] == "done" and not finished_seen:
        return _audit_break(
            None, "finished_event_missing",
            expected="finished event for done task",
        )
    try:
        req = json.loads(task["request"])
    except (ValueError, TypeError):
        return _audit_break(None, "request_not_json", actual=task["request"])
    if not isinstance(req, dict):
        return _audit_break(None, "request_not_object")
    if req.get("use_quota") and "quota_settled" not in seen_phases:
        return _audit_break(
            None, "quota_settled_event_missing",
            expected="quota_settled event for use_quota task",
        )
    if "quota_settled" in seen_phases and not req.get("use_quota"):
        return _audit_break(
            None, "unexpected_quota_settled",
            expected="no quota_settled event without use_quota",
        )
    return None


def get_audit_snapshot(
    task_id: int,
) -> Optional[Tuple[sqlite3.Row, List[sqlite3.Row], Optional[Dict[str, Any]]]]:
    """在**单一只读事务快照**中读取任务行、全部事件、预留明细并完成重放校验。

    任务不存在返回 None；否则返回 (任务行, 按 seq 升序的事件, 首个断点或 None)。
    所有读取共用同一连接的同一事务，校验所见的 tasks / task_audit_events /
    quota_reservations 严格处于同一时刻，杜绝跨连接瞬时不一致造成的误报。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        task = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if task is None:
            conn.commit()
            return None
        events = conn.execute(
            "SELECT task_id, seq, event_type, created_at, payload, prev_hash, hash"
            " FROM task_audit_events WHERE task_id = ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        reservations = conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY question_id",
            (task_id,),
        ).fetchall()
        break_point = _verify_audit_chain(task, events, reservations)
        conn.commit()
        return task, events, break_point
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def verify_audit_chain(
    task_id: int, task: sqlite3.Row, events: List[sqlite3.Row]
) -> Optional[Dict[str, Any]]:
    """便捷校验入口（独立快照读取预留明细）；HTTP 接口请用 get_audit_snapshot。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        reservations = conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY question_id",
            (task_id,),
        ).fetchall()
        result = _verify_audit_chain(task, events, reservations)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _verify_quota_settlement(
    task: sqlite3.Row,
    payload: Dict[str, Any],
    rows: List[sqlite3.Row],
) -> Optional[Dict[str, Any]]:
    """交叉校验单个 quota_settled 事件与同快照内的实际扣减结果。"""
    outcome = payload.get("outcome")
    actual_items = [
        {
            "question_id": json.loads(r["question_id"]),
            "reserved": int(r["reserved"]),
            "remaining": int(r["remaining"]),
        }
        for r in rows
    ]

    result = json.loads(task["result"]) if task["result"] else {}
    if outcome == "reserved":
        # 已扣减：tasks.result 必须仍可行，且逐题预留与事件载荷完全一致
        if result.get("status") != "feasible":
            return {
                "kind": "settlement_outcome_mismatch",
                "expected": "feasible result for reserved settlement",
                "actual": result.get("status"),
            }
        expected_items = payload.get("items")
        well_formed = isinstance(expected_items, list) and all(
            isinstance(it, dict) for it in expected_items
        )
        if (
            not well_formed
            or _canonical_json(sorted(
                expected_items,
                key=lambda it: _canonical_json(it.get("question_id")),
            ))
            != _canonical_json(sorted(
                actual_items,
                key=lambda it: _canonical_json(it.get("question_id")),
            ))
        ):
            return {
                "kind": "reservations_mismatch",
                "expected": expected_items,
                "actual": actual_items,
            }
    elif outcome == "exceeded":
        # 未扣减：不得存在任何预留记录，结果须为 quota_exceeded，缺额明细须一致
        if actual_items:
            return {
                "kind": "reservations_mismatch",
                "expected": [],
                "actual": actual_items,
            }
        if result.get("status") != "quota_exceeded":
            return {
                "kind": "settlement_outcome_mismatch",
                "expected": "quota_exceeded result",
                "actual": result.get("status"),
            }
        if _canonical_json(payload.get("shortages")) != _canonical_json(
            result.get("shortages")
        ):
            return {
                "kind": "shortages_mismatch",
                "expected": payload.get("shortages"),
                "actual": result.get("shortages"),
            }
    else:
        return {
            "kind": "invalid_payload",
            "expected": "outcome in ('reserved', 'exceeded')",
            "actual": outcome,
        }
    return None


# --------------------------------------------------------------------------- #
# 组卷模板（不可变；版本谱系以 root_id 标识，version 在谱系内单调递增）
# --------------------------------------------------------------------------- #
def create_template(
    name: Optional[str], config_json: str, base: Optional[sqlite3.Row]
) -> Tuple[int, int]:
    """插入一条模板记录，返回 (template_id, version)。

    base 为 None 时创建全新谱系（version=1，root_id 指向自身）；
    否则在 base 所在谱系内取当前最大版本号 +1，base_id 记录派生来源。
    模板记录只插不改，天然满足“创建后不可原地修改”。
    """
    with _lock, get_conn() as conn:
        if base is None:
            cur = conn.execute(
                "INSERT INTO templates (name, config, version, root_id, base_id)"
                " VALUES (?, ?, 1, -1, NULL)",
                (name, config_json),
            )
            template_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE templates SET root_id = ? WHERE id = ?",
                (template_id, template_id),
            )
            return template_id, 1

        root_id = int(base["root_id"])
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM templates WHERE root_id = ?",
            (root_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        cur = conn.execute(
            "INSERT INTO templates (name, config, version, root_id, base_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, config_json, version, root_id, int(base["id"])),
        )
        return int(cur.lastrowid), version


def get_template(template_id: int) -> Optional[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM templates WHERE id = ?", (template_id,)
        ).fetchone()


# --------------------------------------------------------------------------- #
# 题目曝光配额（按题号的可用次数；扣减在单个事务中串行化校验、原子生效）
# --------------------------------------------------------------------------- #
def _qkey(qid: Any) -> str:
    """题号的数据库存储键：JSON 编码，区分整数 1 与字符串 "1"。"""
    return json.dumps(qid, ensure_ascii=False)


def set_quotas(items: List[Tuple[Any, int]]) -> None:
    """按题号设置可用次数（绝对值，存在即覆盖）。"""
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for qid, quota in items:
            conn.execute(
                "INSERT INTO quotas (question_id, quota, updated_at)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(question_id) DO UPDATE SET"
                "   quota = excluded.quota, updated_at = excluded.updated_at",
                (_qkey(qid), int(quota)),
            )


def list_quotas() -> List[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT question_id, quota, updated_at FROM quotas ORDER BY rowid"
        ).fetchall()


def get_quota_reservations(task_id: int) -> List[sqlite3.Row]:
    """某任务本次成功扣减的预留明细（按写入顺序，即规范化题号序）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT question_id, reserved, remaining FROM quota_reservations"
            " WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        ).fetchall()


def finish_task_with_quota(
    task_id: int, result: Dict[str, Any], requirements: List[Tuple[Any, int]]
) -> Optional[Dict[str, Any]]:
    """启用配额的任务落库：校验 -> 原子扣减 -> 完成任务 -> 追加审计事件。

    扣减、任务结果更新与 finished / quota_settled 两个审计事件全部在**单个
    SQLite 事务**中，提交则同时生效、回滚则同时消失：配额结算事件绝不与扣减
    结果出现半成功状态。仅**当前为 running 且本执行者已抢占**的任务可结算；
    任务不存在或已 done 时直接返回 None、绝不触碰配额——配合 claim_task 的
    条件抢占，恢复与新提交/重复启动并发时同一任务的配额**只结算一次**。

    requirements 为 (题号, 需扣减次数) 列表。进程内互斥锁 + BEGIN IMMEDIATE
    保证并发任务的校验-扣减严格串行：
    - 全部题余额充足：逐题扣减、写入预留记录（remaining 为扣后快照），
      任务以原可行结果完成，结算事件 outcome=reserved 并冻结逐题扣减/剩余快照；
    - 任一题余额不足（无配额记录按 0 计）：整组不扣减，任务结果改写为
      quota_exceeded 并附缺额明细，结算事件 outcome=exceeded 并冻结缺额明细。
    返回最终落库的 result；任务已无权结算（不存在/已完成）时返回 None。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        owner = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if owner is None or owner["status"] != "running":
            # 已被其他执行者完成或任务不存在：不得扣减任何配额、不得追加事件
            conn.commit()
            return None
        keys = [(_qkey(qid), qid, int(need)) for qid, need in requirements]
        rows = (
            conn.execute(
                "SELECT question_id, quota FROM quotas WHERE question_id IN"
                f" ({','.join('?' for _ in keys)})",
                [k for k, _, _ in keys],
            ).fetchall()
            if keys
            else []
        )
        balance = {r["question_id"]: int(r["quota"]) for r in rows}

        shortages = []
        for key, qid, need in keys:
            available = balance.get(key, 0)
            if available < need:
                shortages.append(
                    {
                        "question_id": qid,
                        "required": need,
                        "available": available,
                        "shortage": need - available,
                    }
                )

        if shortages:
            final = {
                "status": "quota_exceeded",
                "message": "题目曝光配额不足，本次组卷未扣减任何配额",
                "shortages": shortages,
            }
        else:
            settled_items = []
            for key, qid, need in keys:
                remaining = balance[key] - need
                conn.execute(
                    "UPDATE quotas SET quota = ?, updated_at = datetime('now')"
                    " WHERE question_id = ?",
                    (remaining, key),
                )
                conn.execute(
                    "INSERT INTO quota_reservations"
                    " (task_id, question_id, reserved, remaining)"
                    " VALUES (?, ?, ?, ?)",
                    (task_id, key, need, remaining),
                )
                settled_items.append(
                    {"question_id": qid, "reserved": need, "remaining": remaining}
                )
            final = result

        final_json = json.dumps(final, ensure_ascii=False)
        cur_done = conn.execute(
            "UPDATE tasks SET status = 'done', result = ?"
            " WHERE id = ? AND status = 'running'",
            (final_json, task_id),
        )
        if cur_done.rowcount != 1:
            # 进入本事务后被其他执行者抢先完成：整事务回滚，配额不扣、事件不追加
            conn.rollback()
            return None
        # 预留租约与扣减结果在同一事务流转，杜绝“已扣减但租约未生效”的中间态：
        # 成功扣减则激活租约（reserved，expires_at = 当前 UTC + lease_seconds），
        # 配额不足则租约作废（void）；未声明租约的任务无此行，语句不命中任何行
        lease_row = conn.execute(
            "SELECT lease_seconds FROM quota_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if lease_row is not None:
            if shortages:
                conn.execute(
                    "UPDATE quota_leases SET status = 'void' WHERE task_id = ?"
                    " AND status = 'awaiting'",
                    (task_id,),
                )
            else:
                expires_at = (
                    datetime.now(timezone.utc).replace(microsecond=0)
                    + timedelta(seconds=int(lease_row["lease_seconds"]))
                ).isoformat().replace("+00:00", "Z")
                conn.execute(
                    "UPDATE quota_leases SET status = 'reserved', expires_at = ?"
                    " WHERE task_id = ? AND status = 'awaiting'",
                    (expires_at, task_id),
                )
        # finished：冻结任务最终结果摘要（含 quota_exceeded）
        append_audit_event(
            conn,
            task_id,
            "finished",
            {
                "result_status": final.get("status"),
                "result_sha256": _sha256_text(final_json),
            },
        )
        # quota_settled：与上面的扣减/未扣减结果同一事务原子提交
        if shortages:
            append_audit_event(
                conn,
                task_id,
                "quota_settled",
                {"outcome": "exceeded", "shortages": shortages},
            )
        else:
            append_audit_event(
                conn,
                task_id,
                "quota_settled",
                {"outcome": "reserved", "items": settled_items},
            )
        conn.commit()
    return final


# --------------------------------------------------------------------------- #
# 预留租约：查询、到期前主动确认、过期回收（全部在单事务中按条件更新，幂等）
# --------------------------------------------------------------------------- #
def lease_out(row: sqlite3.Row) -> Dict[str, Any]:
    """把 quota_leases 行转换为接口输出字典（status 为含到期判断的有效状态）。"""
    status = row["status"]
    expires_at = row["expires_at"]
    # 逻辑到期：DB 内仍是 reserved 但已过 expires_at——回收接口未必刚跑过；
    # 查询侧据此报告 expired，实际配额释放仍只由回收接口完成（避免双重释放）
    if (
        status == "reserved"
        and expires_at is not None
        and expires_at <= _now_iso()
    ):
        status = "expired"
    return {
        "status": status,
        "lease_seconds": int(row["lease_seconds"]),
        "expires_at": expires_at,
        "confirmed_at": row["confirmed_at"],
        "released_at": row["released_at"],
    }


def get_quota_lease(task_id: int) -> Optional[sqlite3.Row]:
    """读取某任务的租约原始行（无租约返回 None）。"""
    with get_conn() as conn:
        return conn.execute(
            "SELECT task_id, reservation_token, lease_seconds, status,"
            " expires_at, confirmed_at, released_at, created_at"
            " FROM quota_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()


def confirm_quota_reservation(
    task_id: int, token: str
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """持 reservation_token 在到期前主动确认预留。

    返回 (结果码, 租约输出或 None)，结果码：
    - ``no_lease``   ：任务没有租约（非租约任务或不存在配额设置）；
    - ``bad_token``  ：令牌不匹配（不泄露租约当前状态）；
    - ``confirmed``  ：本次确认成功（reserved -> confirmed）；
    - ``already_confirmed``：此前已确认（幂等，配额只扣一次）；
    - ``expired``    ：租约已过期（DB 状态 expired/reclaimed，或 reserved 但
      expires_at 已过）；过期确认绝不释放配额，只由回收接口释放；
    - ``awaiting``   ：任务尚未结算、预留尚未生效；
    - ``void``       ：任务未产生预留（无解/失败/配额不足），租约已作废。

    确认走**带状态与到期条件的单条 UPDATE**（进程内互斥锁 + BEGIN IMMEDIATE）：
    与回收接口并发时只有一方的条件更新能命中行，确认与返还严格互斥。
    """
    now = _now_iso()
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT reservation_token, lease_seconds, status, expires_at,"
            " confirmed_at, released_at FROM quota_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.commit()
            return "no_lease", None
        if not secrets.compare_digest(row["reservation_token"], token):
            conn.commit()
            return "bad_token", None

        status = row["status"]
        if status == "reserved" and row["expires_at"] is not None and row["expires_at"] <= now:
            # 到期时刻只用于拒绝确认；不在确认路径释放配额（释放只能经回收接口）
            code = "expired"
        elif status == "reserved":
            cur = conn.execute(
                "UPDATE quota_leases SET status = 'confirmed', confirmed_at = ?"
                " WHERE task_id = ? AND status = 'reserved' AND (expires_at IS NULL"
                " OR expires_at > ?)",
                (now, task_id, now),
            )
            # 行计数是与回收/并发确认互斥的唯一裁决：命中 1 行才算本次确认
            code = "confirmed" if cur.rowcount == 1 else "expired"
        elif status == "confirmed":
            code = "already_confirmed"
        elif status in ("expired", "reclaimed"):
            code = "expired"
        elif status == "void":
            code = "void"
        else:  # awaiting
            code = "awaiting"

        final_row = conn.execute(
            "SELECT lease_seconds, status, expires_at, confirmed_at, released_at"
            " FROM quota_leases WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        conn.commit()
        return code, lease_out(final_row)


def reclaim_expired_leases() -> Dict[str, Any]:
    """回收所有已过期且未确认的预留：在**单个 SQLite 事务**中原子释放并标记。

    流程（全程持进程内互斥锁 + BEGIN IMMEDIATE，与确认接口/其它扣减串行）：
    1. 选出 status='reserved' 且 expires_at <= 当前 UTC 的租约（到期判定只认
       expires_at，不读可能被改写的时钟之外的状态）；
    2. 对每个任务先按条件抢占式 ``UPDATE ... WHERE status='reserved' AND
       expires_at <= now``——只有行计数为 1（本事务赢得竞争）才返还配额；
    3. 返还：按 quota_reservations 的逐题预留把 reserved 次数加回 quotas
       （配额行在此期间被删除/覆盖时重建为返还值），随后才把租约置为
       reclaimed 并记录 released_at；返还、状态更新、其它任务的回收全部在
       同一事务提交——要么整体生效，要么整体回滚。

    重复调用、确认与回收并发均幂等：已 reclaimed/confirmed 的行永远不再命中
    条件更新，因此同一预留的配额不可能被释放两次。
    返回 {"reclaimed_count", "returned_total", "reclaimed": [...]}。
    """
    now = _now_iso()
    reclaimed_items: List[Dict[str, Any]] = []
    returned_total = 0
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT task_id FROM quota_leases"
            " WHERE status = 'reserved' AND expires_at IS NOT NULL"
            " AND expires_at <= ? ORDER BY task_id",
            (now,),
        ).fetchall()
        for r in rows:
            task_id = int(r["task_id"])
            # 抢占：状态 + 到期双条件。并发回收/确认只有一方能把 rowcount 置 1
            cur = conn.execute(
                "UPDATE quota_leases SET status = 'reclaimed', released_at = ?"
                " WHERE task_id = ? AND status = 'reserved' AND expires_at <= ?",
                (now, task_id, now),
            )
            if cur.rowcount != 1:
                continue
            items = conn.execute(
                "SELECT question_id, reserved FROM quota_reservations"
                " WHERE task_id = ? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            for it in items:
                need = int(it["reserved"])
                bal = conn.execute(
                    "SELECT quota FROM quotas WHERE question_id = ?",
                    (it["question_id"],),
                ).fetchone()
                restored = (int(bal["quota"]) if bal is not None else 0) + need
                conn.execute(
                    "INSERT INTO quotas (question_id, quota, updated_at)"
                    " VALUES (?, ?, datetime('now'))"
                    " ON CONFLICT(question_id) DO UPDATE SET"
                    "   quota = excluded.quota, updated_at = excluded.updated_at",
                    (it["question_id"], restored),
                )
                reclaimed_items.append(
                    {
                        "task_id": task_id,
                        "question_id": json.loads(it["question_id"]),
                        "returned": need,
                    }
                )
                returned_total += need
    return {
        "reclaimed_count": len({i["task_id"] for i in reclaimed_items}),
        "returned_total": returned_total,
        "reclaimed": reclaimed_items,
    }


# --------------------------------------------------------------------------- #
# 录分：已完成可行任务首个候选卷组上的初评/复评/仲裁（乐观版本，事务内原子写入）
# --------------------------------------------------------------------------- #
def _score_row(
    conn: sqlite3.Connection,
    task_id: int,
    paper: int,
    examinee_key: str,
    question_key: str,
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT task_id, paper, examinee_id, question_id, initial_score,"
        " review_score, arbitration_score, version, updated_at"
        " FROM paper_scores WHERE task_id = ? AND paper = ?"
        " AND examinee_id = ? AND question_id = ?",
        (task_id, paper, examinee_key, question_key),
    ).fetchone()


def _score_out(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    return {
        "task_id": int(row["task_id"]),
        "paper": int(row["paper"]),
        "examinee_id": json.loads(row["examinee_id"]),
        "question_id": json.loads(row["question_id"]),
        "initial_score": row["initial_score"],
        "review_score": row["review_score"],
        "arbitration_score": row["arbitration_score"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def put_paper_score(
    task_id: int,
    paper: int,
    examinee_key: str,
    question_key: str,
    stage: str,
    score: int,
    expected_version: Optional[int],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """录入/修改某考生某题的初评或复评分（单事务原子生效）。

    返回 (结果码, 最新行输出或 None)，结果码：
    - ``recorded``       ：新题首次录分（version 仍为 0）；
    - ``filled``         ：补评——此前该评分槽为空（初评缺则补初评），版本不变；
    - ``changed``        ：改分成功，version+1，且旧仲裁在同语句立即撤销；
    - ``version_missing``：槽内已有分（属改分）但未携带 version；
    - ``version_conflict``：携带的 version 与当前版本不符，未写入任何内容。

    新旧分相同视为幂等重试（``changed`` 但不递增、不撤仲裁，行仍原样返回）。
    """
    score_col = "initial_score" if stage == "initial" else "review_score"
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _score_row(conn, task_id, paper, examinee_key, question_key)
        if row is None:
            conn.execute(
                "INSERT INTO paper_scores"
                " (task_id, paper, examinee_id, question_id,"
                "  initial_score, review_score, arbitration_score, version, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, 0, datetime('now'))",
                (
                    task_id, paper, examinee_key, question_key,
                    score if stage == "initial" else None,
                    score if stage == "review" else None,
                ),
            )
            code = "recorded"
        elif row[score_col] is None:
            # 补评：此前缺该轮评分，填补空槽不动版本（每题版本仍从 0 起）
            conn.execute(
                f"UPDATE paper_scores SET {score_col} = ?, updated_at = datetime('now')"
                " WHERE task_id = ? AND paper = ? AND examinee_id = ? AND question_id = ?",
                (score, task_id, paper, examinee_key, question_key),
            )
            code = "filled"
        else:
            current_version = int(row["version"])
            if expected_version is None:
                conn.commit()
                return "version_missing", _score_out(row)
            if int(expected_version) != current_version:
                conn.commit()
                return "version_conflict", _score_out(row)
            if int(row[score_col]) == int(score):
                # 幂等重试：同分再次提交，不递增、不撤销仲裁
                conn.commit()
                return "changed", _score_out(row)
            cur = conn.execute(
                f"UPDATE paper_scores SET {score_col} = ?, arbitration_score = NULL,"
                " version = version + 1, updated_at = datetime('now')"
                " WHERE task_id = ? AND paper = ? AND examinee_id = ? AND question_id = ?"
                " AND version = ?",
                (score, task_id, paper, examinee_key, question_key, current_version),
            )
            # 条件 UPDATE 的行计数是并发裁决：0 行即被抢先改动，绝不覆盖
            if cur.rowcount != 1:
                conn.commit()
                latest = _score_row(conn, task_id, paper, examinee_key, question_key)
                return "version_conflict", _score_out(latest)
            code = "changed"
        final = _score_row(conn, task_id, paper, examinee_key, question_key)
        conn.commit()
        return code, _score_out(final)


def arbitrate_paper_score(
    task_id: int,
    paper: int,
    examinee_key: str,
    question_key: str,
    score: int,
    expected_version: Optional[int],
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """录入/修改仲裁分（单事务原子生效）。允许仲裁（两评分齐备且相差 >1）的
    前置校验在 HTTP/scoring 层完成；本函数只负责乐观版本写入。

    返回 (结果码, 最新行输出或 None)：
    - ``arbitrated``      ：写入成功（含与现有仲裁同分的幂等重试，幂等时不递增）；
    - ``no_score_row``    ：尚无初评/复评记录；
    - ``version_missing`` ：未携带 version；
    - ``version_conflict``：版本不符，未写入。
    """
    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _score_row(conn, task_id, paper, examinee_key, question_key)
        if row is None:
            conn.commit()
            return "no_score_row", None
        current_version = int(row["version"])
        if expected_version is None:
            conn.commit()
            return "version_missing", _score_out(row)
        if int(expected_version) != current_version:
            conn.commit()
            return "version_conflict", _score_out(row)
        if row["arbitration_score"] is not None and int(row["arbitration_score"]) == int(score):
            conn.commit()
            return "arbitrated", _score_out(row)
        cur = conn.execute(
            "UPDATE paper_scores SET arbitration_score = ?, version = version + 1,"
            " updated_at = datetime('now')"
            " WHERE task_id = ? AND paper = ? AND examinee_id = ? AND question_id = ?"
            " AND version = ?",
            (score, task_id, paper, examinee_key, question_key, current_version),
        )
        if cur.rowcount != 1:
            conn.commit()
            latest = _score_row(conn, task_id, paper, examinee_key, question_key)
            return "version_conflict", _score_out(latest)
        final = _score_row(conn, task_id, paper, examinee_key, question_key)
        conn.commit()
        return "arbitrated", _score_out(final)


def get_paper_score_rows(
    task_id: int, paper: int, examinee_key: str
) -> List[sqlite3.Row]:
    """在**单一只读事务快照**中读取某考生某卷的全部录分行（按主键顺序）。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        rows = conn.execute(
            "SELECT task_id, paper, examinee_id, question_id, initial_score,"
            " review_score, arbitration_score, version, updated_at"
            " FROM paper_scores WHERE task_id = ? AND paper = ? AND examinee_id = ?"
            " ORDER BY rowid",
            (task_id, paper, examinee_key),
        ).fetchall()
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 成绩批次发布：单事务快照校验 + 一次性冻结（只追加，发布后不可变）
# --------------------------------------------------------------------------- #
def _batch_out(
    conn: sqlite3.Connection,
    task_id: int,
    paper: int,
    batch_key: str,
    header: Optional[sqlite3.Row] = None,
) -> Dict[str, Any]:
    """在调用方事务/连接上读取批次头与逐考生明细，组装冻结快照输出。"""
    if header is None:
        header = conn.execute(
            "SELECT task_id, paper, batch_id, examinees_json, published_at"
            " FROM score_batches WHERE task_id = ? AND paper = ? AND batch_id = ?",
            (task_id, paper, batch_key),
        ).fetchone()
    item_rows = conn.execute(
        "SELECT sheet_json FROM score_batch_items"
        " WHERE task_id = ? AND paper = ? AND batch_id = ? ORDER BY seq",
        (task_id, paper, batch_key),
    ).fetchall()
    return {
        "task_id": int(header["task_id"]),
        "paper": int(header["paper"]),
        "batch_id": json.loads(header["batch_id"]),
        "status": "frozen",
        "examinee_ids": json.loads(header["examinees_json"]),
        "examinees": [json.loads(r["sheet_json"]) for r in item_rows],
        "published_at": header["published_at"],
    }


def publish_score_batch(
    task_id: int,
    paper: int,
    batch_key: str,
    examinee_keys: List[str],
    selected_ids: List[Any],
) -> Tuple[str, Any]:
    """在**单个写事务**内校验名单并一次性冻结全批成绩。

    全程持进程内互斥锁 + BEGIN IMMEDIATE，与录分写操作互斥；校验读取与冻结
    写入共用同一事务快照，杜绝“校验时有效、冻结前被改分”的中间态：

    1. 批次已存在（同 task/paper/batch_id）：名单的规范化 JSON 与冻结名单逐
       位置、逐标识一致 → 返回原快照（``exists``，幂等重试）；不一致 →
       ``conflict``（附冻结名单，供 409 响应），不写入任何内容；
    2. 批次不存在：同一快照读取名单内**全部**考生该卷的录分行，按题序逐题套用
       有效分口径。任一考生任一缺评/待仲裁题都记入 blockers 并**回滚整事务**
       （``pending``，整批不发布）；
    3. 每人每题均有有效分：插入批次头与逐考生冻结明细（逐题有效分 + 总分），
       随事务原子提交（``created``）。此后初评/复评改分或仲裁只影响
       paper_scores，本批次快照永不改变。

    ``examinee_keys`` / ``batch_key`` 均为 JSON 编码后的标识（整数 1 与字符串
    "1" 严格区分）；``selected_ids`` 为首个候选卷组该卷的题号顺序。
    """
    from . import scoring  # 局部导入：复用既有有效分口径，保持层次单向

    with _lock, get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT examinees_json FROM score_batches"
            " WHERE task_id = ? AND paper = ? AND batch_id = ?",
            (task_id, paper, batch_key),
        ).fetchone()
        incoming_ids = [json.loads(k) for k in examinee_keys]
        incoming_canonical = _canonical_json(incoming_ids)
        if existing is not None:
            conn.commit()
            if existing["examinees_json"] == incoming_canonical:
                return "exists", _batch_out(conn, task_id, paper, batch_key)
            return "conflict", json.loads(existing["examinees_json"])

        # 同快照一次性读取名单内全部考生的录分行，再按考生分组
        placeholders = ",".join("?" for _ in examinee_keys)
        rows = conn.execute(
            "SELECT examinee_id, question_id, initial_score, review_score,"
            " arbitration_score, version"
            " FROM paper_scores WHERE task_id = ? AND paper = ?"
            f" AND examinee_id IN ({placeholders})",
            [task_id, paper, *examinee_keys],
        ).fetchall()
        rows_by_examinee: Dict[str, Dict[str, sqlite3.Row]] = {}
        for r in rows:
            rows_by_examinee.setdefault(r["examinee_id"], {})[
                json.loads(r["question_id"])
            ] = r

        blockers: List[Dict[str, Any]] = []
        sheets: List[Dict[str, Any]] = []
        for examinee_key in examinee_keys:
            examinee_id = json.loads(examinee_key)
            by_question = rows_by_examinee.get(examinee_key, {})
            questions: List[Dict[str, Any]] = []
            total: float = 0
            blocked = False
            for qid in selected_ids:
                state = scoring.question_state(by_question.get(qid))
                effective = state["effective_score"]
                if effective is None:
                    # 每名考生只报第一道未决题（题序即 selected_ids），整批随后回滚
                    blockers.append(
                        {
                            "examinee_id": examinee_id,
                            "question_id": qid,
                            "reason": state["pending_reason"],
                            "message": state["pending_message"],
                        }
                    )
                    blocked = True
                    break
                questions.append({"question_id": qid, "effective_score": effective})
                total += effective
            if blocked:
                continue
            if float(total).is_integer():
                total = int(total)
            sheets.append(
                {
                    "examinee_id": examinee_id,
                    "questions": questions,
                    "total_score": total,
                }
            )

        if blockers:
            # 缺评或待仲裁：整批不发布，事务回滚不留任何批次痕迹
            conn.rollback()
            return "pending", blockers

        published_at = _now_iso()
        conn.execute(
            "INSERT INTO score_batches"
            " (task_id, paper, batch_id, examinees_json, published_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (task_id, paper, batch_key, incoming_canonical, published_at),
        )
        for seq, sheet in enumerate(sheets):
            conn.execute(
                "INSERT INTO score_batch_items"
                " (task_id, paper, batch_id, seq, examinee_id, sheet_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    paper,
                    batch_key,
                    seq,
                    scoring.encode_id_key(sheet["examinee_id"]),
                    _canonical_json(sheet),
                ),
            )
        conn.commit()
        header = conn.execute(
            "SELECT task_id, paper, batch_id, examinees_json, published_at"
            " FROM score_batches WHERE task_id = ? AND paper = ? AND batch_id = ?",
            (task_id, paper, batch_key),
        ).fetchone()
        return "created", _batch_out(conn, task_id, paper, batch_key, header)


def get_score_batch(
    task_id: int, paper: int, batch_key: str
) -> Optional[Dict[str, Any]]:
    """在单一只读事务快照中读取一个已发布批次（批次不存在返回 None）。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        header = conn.execute(
            "SELECT task_id, paper, batch_id, examinees_json, published_at"
            " FROM score_batches WHERE task_id = ? AND paper = ? AND batch_id = ?",
            (task_id, paper, batch_key),
        ).fetchone()
        if header is None:
            conn.commit()
            return None
        result = _batch_out(conn, task_id, paper, batch_key, header)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
