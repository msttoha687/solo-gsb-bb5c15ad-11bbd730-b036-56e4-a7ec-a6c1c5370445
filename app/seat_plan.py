"""考场卷别排布求解器：OR-Tools CP-SAT。

在已完成且结果可行、含 2~5 份平行卷的组卷任务之上，把各卷按其印量排布到
考场座位上：每个可用座位恰好放 1 份卷、每份卷恰好放满其印量；不可用座位
（缺考/隔离位等）空置不参与排布。

接触边数：对每条上下左右相邻的座位对，两端放置**同一卷别**时计 1 条接触边，
不同卷别（或任一端为空位）不计。目标是在严格满足各卷印量的前提下使接触边
总数最少（尽量打散同卷相邻）。

同最优值的固定规则：按**行优先、卷号升序**唯一确定排布——把行优先展平后的
卷号序列按字典序最小化（第一个不同的座位上卷号尽可能小），分块加权编码处理，
避免指数权重溢出，同时唯一破缺座位与卷别之间的对称性。

入参印量总和由接口层保证等于可用座位数（Pydantic 校验，不等即 422），因此
约束本身恒可行。择优分两个阶段求解，**任一阶段在限时内只找到可行解而未证明
最优（或完全无法判定）时都返回 error，绝不返回部分方案**：

  - 阶段 1 必须证明接触边总数的最优值（仅 FEASIBLE 不被接受，其目标值可能
    不是全局最小）；
  - 阶段 2 的每个行优先破平分块都必须在固定此前各块的前提下证明本块最优，
    首个未完成分块之前的排布不能沿用——它只对已固定的分块字典序最小，整段
    卷号序列未必满足字典序规则。

全部求解单线程、固定随机种子，同一任务同一请求结果可复现。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from ortools.sat.python import cp_model

CHUNK_BITS = 20  # 卷号序列分块编码的块大小（与组卷/盲审求解器一致）
STAGE_TIME_LIMIT = 10.0  # 每个择优阶段的求解上限（秒）


def _build_model(
    rows: int,
    cols: int,
    available: List[Tuple[int, int]],
    quantities: List[int],
) -> Tuple[cp_model.CpModel, List[List[cp_model.IntVar]], cp_model.LinearExpr]:
    """构造排布模型。

    变量 z[s][p] 表示行优先第 s 个可用座位是否放第 p 卷（每座恰好一卷，
    每卷恰好放满印量）；e[k][p] 表示第 k 条上下左右相邻边的两端是否同为
    第 p 卷。返回 (模型, z, 接触边总数表达式)。
    """
    P = len(quantities)
    m = cp_model.CpModel()

    z = [[m.NewBoolVar(f"z_{s}_{p}") for p in range(P)] for s in range(len(available))]

    # 每座恰好 1 份卷；每卷恰好放满其印量
    for s in range(len(available)):
        m.Add(sum(z[s][p] for p in range(P)) == 1)
    for p in range(P):
        m.Add(sum(z[s][p] for s in range(len(available))) == quantities[p])

    # 上下左右相邻边（仅统计两端都是可用座位的边；任一端为不可用座位不计）
    pos = {(r, c): s for s, (r, c) in enumerate(available)}
    adjacency: List[Tuple[int, int]] = []
    for s, (r, c) in enumerate(available):
        down = pos.get((r + 1, c))
        if down is not None:
            adjacency.append((s, down))
        right = pos.get((r, c + 1))
        if right is not None:
            adjacency.append((s, right))

    # e[k][p] = 1  iff 相邻边 k 的两端同为第 p 卷
    edge_terms: List[cp_model.LinearExpr] = []
    for k, (a, b) in enumerate(adjacency):
        for p in range(P):
            same = m.NewBoolVar(f"e_{k}_{p}")
            m.AddBoolAnd([z[a][p], z[b][p]]).OnlyEnforceIf(same)
            m.AddBoolOr([z[a][p].Not(), z[b][p].Not()]).OnlyEnforceIf(same.Not())
            edge_terms.append(same)

    contact = sum(edge_terms)
    return m, z, contact


def _chunk_expr(
    z: List[List[cp_model.IntVar]], chunk: int, width: int, P: int
) -> cp_model.LinearExpr:
    """行优先卷号序列第 chunk 块的加权编码。

    每座卷号直接取 0..P-1 的整数（z 为每行恰有一个 1 的 0/1 行，其加权和
    Σ_p p·z 即卷号）；块内按行优先顺序以小座位为高位做 P 进制加权，最小化
    全部块编码（按块序逐块固定）等价于卷号序列的字典序最小。
    width 为块内实际座位数（末块可能不足 CHUNK_BITS），权重 P^(width-1-q)
    在 width <= CHUNK_BITS、P <= 5 时远在 int64 安全范围内。
    """
    lo = chunk * CHUNK_BITS
    terms: List[cp_model.LinearExpr] = []
    for q, s in enumerate(range(lo, min(lo + width, len(z)))):
        paper = sum(p * z[s][p] for p in range(P))
        terms.append(paper * (P ** (width - 1 - q)))
    return sum(terms)


def _solver(seed: int) -> cp_model.CpSolver:
    s = cp_model.CpSolver()
    s.parameters.num_search_workers = 1  # 单线程保证确定性
    s.parameters.random_seed = seed
    s.parameters.max_time_in_seconds = STAGE_TIME_LIMIT
    return s


def solve_seat_plan(
    rows: int,
    cols: int,
    unavailable: List[Tuple[int, int]],
    quantities: List[int],
    seed: int = 42,
) -> Dict[str, Any]:
    """求解考场卷别排布。

    仅当接触边总数最少这一目标与全部行优先破平分块都被证明为最优时，才返回
    可行排布（逐座卷号、空位与接触边数）；任一阶段在限时内仅找到可行解而未
    证明最优、或完全无法判定时，返回 error 结构且不含任何部分排布。

    unavailable 为 (行, 列) 列表（均从 0 起，已由接口层去重并校验在界内）；
    quantities 为按卷号 0..P-1 的各卷印量，长度为卷数（2~5），总和已由
    接口层校验等于可用座位数。
    """
    P = len(quantities)
    blocked = set(unavailable)
    available = [
        (r, c)
        for r in range(rows)
        for c in range(cols)
        if (r, c) not in blocked
    ]
    S = len(available)

    base: Dict[str, Any] = {
        "rows": rows,
        "cols": cols,
        "paper_count": P,
        "quantities": list(quantities),
        "available_seats": S,
        "unavailable_seats": [
            {"row": r, "col": c} for r, c in sorted(blocked)
        ],
    }

    def error_result(message: str) -> Dict[str, Any]:
        # 错误结构只含回显与诊断，绝不携带 grid/seats/contact_edges 等部分排布
        return {"status": "error", **base, "error": message}

    # 阶段 1：同卷别接触边总数最少。
    # 必须取得 OPTIMAL：限时内仅找到可行解（FEASIBLE）时其目标值可能并非全局
    # 最小，把它固定下来会让后续排布满足印量却不满足“最少接触边”择优目标。
    m, z, contact = _build_model(rows, cols, available, quantities)
    m.Minimize(contact)
    s = _solver(seed)
    status = s.Solve(m)
    if status == cp_model.INFEASIBLE:
        # 入参已保证印量总和等于可用座位数，实际不会到达；保守拒绝而非给部分方案
        return {
            "status": "infeasible",
            "message": "不存在满足各卷印量的座位分配方案",
            **base,
        }
    if status != cp_model.OPTIMAL:
        # FEASIBLE（仅找到可行解，未证明最优）/ UNKNOWN（未得解）同样拒绝
        return error_result("求解器在规定时间内无法判定接触边总数的最优值")
    best_contact = int(s.Value(contact))

    # 阶段 2：行优先、卷号升序固定结果——逐块最小化行优先卷号序列的 P 进制编码，
    # 并固定此前各块的最优值；座位/卷别间的对称被唯一破缺，结果可复现。
    # 每个分块都必须取得 OPTIMAL 才算证实其字典序最小：若首个未完成分块在限时内
    # 只得到可行解，沿用上一阶段排布只会对已固定分块最优，整段序列不满足字典序
    # 规则，因此直接返回 error，不返回任何部分排布。
    n_chunks = (S + CHUNK_BITS - 1) // CHUNK_BITS
    fixed: List[Tuple[int, int]] = []  # (块 k, 块值)
    for k in range(n_chunks):
        m, z, contact = _build_model(rows, cols, available, quantities)
        m.Add(contact == best_contact)
        for fk, fv in fixed:
            width = min(CHUNK_BITS, S - fk * CHUNK_BITS)
            m.Add(_chunk_expr(z, fk, width, P) == fv)
        width = min(CHUNK_BITS, S - k * CHUNK_BITS)
        enc = _chunk_expr(z, k, width, P)
        m.Minimize(enc)
        s = _solver(seed)
        status = s.Solve(m)
        if status != cp_model.OPTIMAL:
            return error_result(
                "求解器在规定时间内无法证明行优先卷号序列的字典序最优"
            )
        fixed.append((k, int(s.Value(enc))))

    # 全部择优阶段均已证明最优，取最后一个阶段的解构造逐座结果
    chosen = [[bool(s.Value(z[sx][p])) for p in range(P)] for sx in range(S)]
    papers: List[int] = [
        next(p for p in range(P) if chosen[sx][p]) for sx in range(S)
    ]

    # 逐座结果（含不可用座位为空位），按行优先返回
    seats: List[Dict[str, Any]] = []
    grid: List[List[Optional[int]]] = [
        [None for _ in range(cols)] for _ in range(rows)
    ]
    paper_by_pos: Dict[Tuple[int, int], int] = {}
    for sx, (r, c) in enumerate(available):
        p = papers[sx]
        paper_by_pos[(r, c)] = p
        grid[r][c] = p
        seats.append({"row": r, "col": c, "paper": p})

    # 按最终排布重数接触边（与求解器目标同口径）；与已证明的最优值不符说明
    # 得到的排布不满足最少接触边目标，此时宁可报错也不返回伪最优排布。
    edge_count = 0
    for r in range(rows):
        for c in range(cols):
            if (r, c) not in paper_by_pos:
                continue
            if (r + 1, c) in paper_by_pos and paper_by_pos[(r + 1, c)] == paper_by_pos[(r, c)]:
                edge_count += 1
            if (r, c + 1) in paper_by_pos and paper_by_pos[(r, c + 1)] == paper_by_pos[(r, c)]:
                edge_count += 1
    if edge_count != best_contact:
        return error_result("内部校验失败：最终排布的接触边数与已证明的最优值不一致")

    return {
        "status": "feasible",
        **base,
        "total_seats": rows * cols,
        "grid": grid,
        "seats": seats,
        "empty_seats": [
            {"row": r, "col": c} for r, c in sorted(blocked)
        ],
        "contact_edges": edge_count,
    }
