# 智能组卷 API

面向教研组的约束组卷服务：提交**题库快照**与**总分、题型数量、知识点覆盖、难度区间、互斥题组**等约束，自动生成分值精确、约束满足、结果可复现的试卷；多组可行解按 **难度偏差 → 重复知识点数 → 题目编号序列** 依次择优；无解时返回经逐项删减验证的**最小冲突约束集**。

支持一次请求生成 **2～5 份平行卷**（`paper_count`）：所有卷在同一个 CP-SAT 联合模型中分别满足全部单卷约束，并限制任意两卷共同题数不超过 `max_shared_questions`；多卷可行解按 **各卷难度偏差总和 → 跨卷重复题总数 → 各卷题号序列** 依次择优，无解时冲突集区分**单卷约束**（`per_paper`）与**跨卷重叠上限**（`cross_paper`）。不传 `paper_count` 时为单卷，请求与响应结构完全保持兼容。

还支持一次请求返回 **1～5 个候选卷组**（`alternative_count`，缺省 `1`）：在同一冻结题库与原约束下，按上述既有择优规则依次给出排名候选；后续候选与此前每个候选的**每张对应卷**至少有 `min_question_changes`（多候选缺省 `1`）道题不同，且多卷时**不能仅靠交换卷序**制造新候选。差异约束只增不删、绝不放宽原约束；候选不足时返回实际数量与 `exhausted: true`。`alternative_count` 缺省（或为 `1`）时响应结构与旧版完全一致。

此外支持对已完成且可行的任务**定向改卷**（`POST /api/tasks/{task_id}/revise`）：按卷指定必须保留/移除的题号，联合重求解全部卷并在满足原约束的前提下**最大化各卷保留原题总数**，详见下文[定向改卷](#定向改卷post-apitaskstask_idrevise)。

还提供**盲审分配**（`POST /api/tasks/{task_id}/review-plan`）：为已完成且结果可行的组卷任务把审阅人**联合分配**到各卷——满足同卷不重复、回避（不可审卷号）与总容量约束，并依次最小化**最大个人负载 → 负载相对平均值的绝对偏差总和 → 按卷号展平的审阅人编号序列**，结果可复现；无解时返回各卷可用人力与总容量缺口，绝不返回部分方案，详见下文[盲审分配](#盲审分配post-apitaskstask_idreview-plan)。

还提供**考场卷别排布**（`POST /api/tasks/{task_id}/seat-plan`）：为已完成且**含 2～5 份平行卷**的可行任务，在不超过 6×6 的考场中按各卷印量排布座位——每个可用座位放 1 份卷、每卷放满印量，按上下左右相邻统计**同卷别接触边数**并求全局最少；同最优值按**行优先、卷号升序**唯一固定排布，同步返回逐座卷号、空位与接触边数，详见下文[考场卷别排布](#考场卷别排布post-apitaskstask_idseat-plan)。

还提供**可版本化的组卷模板**（`POST /api/templates`）：把题库无关的约束配置（总分、题型数量、知识点覆盖、难度区间、互斥题组、多卷/候选参数）保存为**创建后不可修改**的模板；用 `POST /api/templates/{template_id}/tasks` 以新的题库快照实例化组卷任务时，可通过白名单 `overrides` 覆盖部分约束，**实际生效配置与 `template_id`/`version` 一并冻结进任务记录**，详见下文[组卷模板](#组卷模板可版本化)。

另提供**题目曝光配额**模块（`PUT /api/quotas`）：按题号设置可用次数；创建任务时声明 `use_quota: true` 后，任务完成可行组卷即在 **SQLite 事务中按题号原子扣减**——并发任务串行化校验，任一题余额不足则整组不扣减、任务返回 `quota_exceeded` 及缺额明细；失败或无解任务不消耗配额。可在创建时附 `lease_seconds` 为本次预留建立**可过期的预留租约**：创建即下发一次性 `reservation_token`，任务完成后可在到期前凭 token **主动确认**；逾期未确认的预留由 `POST /api/quotas/reclaim-expired` 在 **SQLite 事务中原子释放（配额按题返还）并标记过期**，重复确认、过期后确认与并发回收均幂等，同一配额不会释放两次，详见下文[预留租约](#预留租约lease_seconds)。

每个异步任务还附带一条**防篡改审计链**（`GET /api/tasks/{task_id}/audit`）：任务创建、开始、完成或失败、配额结算均在**对应业务写入的同一 SQLite 事务内**追加哈希链接事件（规范化 JSON + SHA-256，含前驱哈希）；接口重放整条链并与任务/配额业务表交叉校验，发现序号、前驱哈希或内容摘要异常时返回**首个断点**，绝不伪报通过，详见下文[防篡改审计链](#防篡改审计链)。

教研组还可对**已完成且可行**的任务**录分**（首个候选卷组，卷号从 0 起）：按 `(卷号, 考生标识, 题号)` 持久化**初评/复评**两轮整数分（0 至该题分值，仅允许该卷题目，整数与字符串题号/考生标识严格区分）。每题版本从 0 起——首次录分与缺轮补评不占版本；**改分与仲裁必须携带当前版本，成功后版本 +1，版本不符返回 409 冲突且不写入**，任何改分立即撤销旧仲裁。初评复评齐备且**相差不超过 1 分**时有效分自动取**均值**（差 1 为半分），否则等待补评或仲裁；**仲裁仅限两评分相差超过 1 分**。查询在同一快照返回逐题两评分、有效分、版本与待处理原因，**全卷每题均有有效分才返回总分**，详见下文[录分与仲裁](#录分与仲裁初评复评仲裁)。

异步任务在**创建事务中即落库**，执行权不依赖进程内守护线程：服务**启动时自动接管**上一进程遗留的 `pending` 任务与心跳陈旧的 `running` 孤儿任务（按**冻结请求、种子与原任务号**恢复求解，涵盖普通、模板实例化与定向改卷任务），运行中另有守护线程周期重扫，也可调用 `POST /api/tasks/recover` 手动触发。领取走数据库条件更新 + 心跳租约，与新提交或重复启动并发时**同一任务只有一个最终结果、启用曝光配额的任务只结算一次**，租约流转与审计链在同一事务保持一致；已完成任务与已冻结成绩批次绝不重算，恢复执行异常落为可查询的 `error` 结果，详见下文[崩溃恢复与任务接管](#崩溃恢复与任务接管post-apitasksrecover)。

技术栈：Python · FastAPI · SQLite · OR-Tools CP-SAT · Docker / Compose。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build        # 前台运行；加 -d 后台运行
```

容器启动时自动安装依赖并创建 SQLite 数据库（含 tasks、templates、quotas、quota_reservations、quota_leases、task_audit_events、paper_scores、score_batches 与 score_batch_items 表），无需任何手动初始化。

- API 地址：http://localhost:8000
- 端口：`8000`（宿主机 `8000` → 容器 `8000`，可在 `docker-compose.yml` 的 `ports` 修改）
- 必要配置（环境变量，均有默认值，一般无需修改）：
  - `EXAM_DB_PATH`：SQLite 数据库文件路径，默认 `/data/exam.db`（已挂载为持久卷 `exam_data`，删除卷 `docker compose down -v` 会清空全部任务、模板、配额与录分数据）
  - `EXAM_TASK_STALE_SECONDS`：running 任务心跳陈旧阈值（秒，默认 `30`）：超过该时长无心跳的任务才会在重启/扫描时被接管
  - `EXAM_TASK_SWEEP_SECONDS`：后台未决任务接管扫描周期（秒，默认 `10`）：服务启动时总会额外立即扫描一轮
- 容器重启或服务重新部署后，上一进程遗留的未决任务（`pending` 及心跳陈旧的 `running`）会在启动时**自动接管恢复**，无需人工干预；也可随时调用 `POST /api/tasks/recover` 手动触发，详见[崩溃恢复与任务接管](#崩溃恢复与任务接管post-apitasksrecover)
- 交互式文档（Swagger）：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/health`（Compose 已配置同路径 healthcheck）
- 查看日志：`docker compose logs -f exam-api`；停止：`docker compose down`

### 本地开发（不用 Docker）

```bash
pip install -r requirements.txt
EXAM_DB_PATH=./exam.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/tasks` | 创建组卷任务（异步），返回 `task_id`；`use_quota: true` 时启用题目曝光配额 |
| `POST` | `/api/tasks/recover` | 手动触发一轮**未决任务接管**：领取 pending 与心跳陈旧的 running 孤儿任务并按冻结请求恢复（服务启动时也自动执行一轮；可重复调用） |
| `POST` | `/api/tasks/{task_id}/revise` | 在已完成且可行的任务上创建**定向改卷**任务（异步） |
| `POST` | `/api/tasks/{task_id}/review-plan` | 为已完成且可行的任务生成**盲审分配**方案（同步返回，不产生新任务） |
| `POST` | `/api/tasks/{task_id}/seat-plan` | 为已完成且含 2～5 份平行卷的可行任务生成**考场卷别排布**（同步返回，不产生新任务） |
| `GET`  | `/api/tasks/{task_id}/audit` | 查询任务的**防篡改审计事件链**及重放校验结论（首个断点） |
| `GET`  | `/api/tasks/{task_id}` | 查询任务状态与结果（启用配额的任务附本次预留及剩余次数） |
| `POST` | `/api/templates` | 创建组卷模板（不可变）；带 `base_template_id` 时创建该谱系的新版本 |
| `GET`  | `/api/templates/{template_id}` | 查询模板的冻结配置与版本信息 |
| `POST` | `/api/templates/{template_id}/tasks` | 用模板 + 新题库快照实例化组卷任务（异步，支持白名单 `overrides` 与 `use_quota`） |
| `PUT`  | `/api/quotas` | 按题号**设置**曝光配额可用次数（绝对值，存在即覆盖） |
| `GET`  | `/api/quotas` | 查询全部题号的当前剩余可用次数 |
| `POST` | `/api/quotas/reclaim-expired` | **回收**所有已过期且未确认的预留租约（原子释放、逐题返还；幂等） |
| `POST` | `/api/tasks/{task_id}/quota/confirm` | 凭 reservation_token **主动确认**预留（重复/过期确认幂等） |
| `PUT`  | `/api/tasks/{task_id}/papers/{paper}/examinees/{examinee}/questions/{question}/scores` | 对已完成可行任务首个候选卷组录入/修改**初评或复评**分（乐观版本，持久化） |
| `POST` | `/api/tasks/{task_id}/papers/{paper}/examinees/{examinee}/questions/{question}/arbitration` | 两评分相差超过 1 分时录入**仲裁分**（须携版本，改分即撤销旧仲裁） |
| `GET`  | `/api/tasks/{task_id}/papers/{paper}/examinees/{examinee}/scores` | 同快照查询某考生某卷逐题两评分、有效分、版本与待处理原因 |
| `PUT` | `/api/tasks/{task_id}/papers/{paper}/score-batches/{batch}` | **按批次发布成绩**：同一数据库快照内全员有效才一次性冻结全批逐题有效分与总分（201 新建 / 200 幂等重试 / 409 名单冲突 / 422 缺评待仲裁） |
| `GET` | `/api/tasks/{task_id}/papers/{paper}/score-batches/{batch}` | 按批次查询**已冻结发布**的成绩快照（发布后改分/仲裁不改历史批次） |
| `GET`  | `/health` | 健康检查 |

任务状态：`pending` → `running` → `done`；结果在 `result` 字段中返回（`feasible` / `infeasible` / `error` / `quota_exceeded`）。状态为 `done` 的任务（及其已冻结成绩批次）在任何接管扫描中都不会被重新执行；服务退出时遗留的 `pending` / `running` 任务由[崩溃恢复与任务接管](#崩溃恢复与任务接管post-apitasksrecover)自动收敛。

## 请求示例

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [
      {"id": 1, "score": 5,  "type": "single_choice", "difficulty": 0.2, "knowledge_points": ["集合"]},
      {"id": 2, "score": 5,  "type": "single_choice", "difficulty": 0.5, "knowledge_points": ["函数"]},
      {"id": 3, "score": 5,  "type": "single_choice", "difficulty": 0.8, "knowledge_points": ["导数"]},
      {"id": 4, "score": 10, "type": "essay",         "difficulty": 0.6, "knowledge_points": ["函数", "导数"]},
      {"id": 5, "score": 10, "type": "essay",         "difficulty": 0.7, "knowledge_points": ["数列"]}
    ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "seed": 42
  }'
```

字段说明：

- `questions`：题库快照，提交后冻结。每题含 `id`（数字或字符串，唯一）、`score`（正整数）、`type`、`difficulty`（0~1）、`knowledge_points`。
- `total_score`：试卷总分，**精确等于**。
- `type_counts`：各题型的精确数量。
- `knowledge_coverage`：知识点 → 试卷中至少出现的题目数。
- `difficulty_range`：试卷平均难度（按分值加权）闭区间 `[min, max]`。
- `target_difficulty`：期望平均难度，缺省取难度区间中点；难度偏差为 `|平均难度 - 目标难度|`。
- `mutex_groups`：互斥题组，每组至多选取 1 题。
- `paper_count`：平行卷份数，取 `2`～`5` 时一次生成多份卷；缺省（或 `1`）为单卷，响应结构与旧版一致。
- `max_shared_questions`：任意两卷**共同题数上限**，仅多卷时可设置；缺省 `0`（平行卷之间不重复）。
- `alternative_count`：候选卷组数量，取 `1`～`5`，缺省 `1`（单候选，响应结构与旧版完全一致）。
- `min_question_changes`：后续候选与此前每个候选的**每张对应卷**至少不同的题目数（按对称差计），多候选时缺省 `1`；多卷任务下仅交换卷序不算新候选。
- `use_quota`：是否启用**题目曝光配额**（缺省 `false`）；启用后任务完成可行组卷时按题号原子扣减配额，详见下文[题目曝光配额](#题目曝光配额use_quota)。
- `lease_seconds`：配额预留租约有效期（正整数秒，缺省不设置）；仅可与 `use_quota: true` 同时给出，创建即返回一次性 `reservation_token`，详见[预留租约](#预留租约lease_seconds)。
- `seed`：随机种子（默认 42），相同请求 + 相同种子结果可复现。

## 多卷请求示例（2～5 份平行卷）

在单卷请求体上增加 `paper_count` 与 `max_shared_questions` 即可。各卷在**同一个联合 CP-SAT 模型**中分别满足总分、题型、知识点覆盖、难度区间、互斥题组约束，且任意两卷共同题数不超过上限：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·A/B/C 平行卷",
    "questions": [
      {"id": 1,  "score": 5,  "type": "single_choice", "difficulty": 0.3, "knowledge_points": ["集合"]}
      // ……题库快照（题量需足够分给各卷，允许部分题目各卷都不选）
    ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "mutex_groups": [[12, 13]],
    "paper_count": 3,
    "max_shared_questions": 1,
    "seed": 42
  }'
```

择优顺序（逐层最优后再优化下一层，保证确定性、与卷的排列无关）：

1. **各卷难度偏差总和**最小；
2. **跨卷重复题总数**（所有卷对共同题数求和）最少；
3. **各卷题号序列**按卷序依次最小（同时唯一确定卷的排列）。

### 多卷可行结果

结果在 `papers` 数组中逐卷返回（每卷字段与单卷结果一致），并附跨卷统计：

```json
{
  "status": "feasible",
  "paper_count": 3,
  "max_shared_questions": 1,
  "papers": [
    {
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2}
    }
  ],
  "total_difficulty_deviation": 0.04,
  "total_shared_questions": 1,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 1, "question_ids": [9]},
    {"papers": [0, 2], "count": 0, "question_ids": []},
    {"papers": [1, 2], "count": 0, "question_ids": []}
  ],
  "seed": 42
}
```

- `papers[i].paper`：第 `i` 卷的完整题目对象（卷序号从 0 起）。
- `total_difficulty_deviation`：各卷 `difficulty_deviation` 之和（第 1 择优目标）。
- `total_shared_questions`：所有卷对共同题数之和（第 2 择优目标），`shared_question_pairs` 给出每对卷的共同题号，每对的 `count` 均不超过 `max_shared_questions`。

### 多卷无解结果（单卷约束 vs 跨卷上限）

多卷无解时，`conflict_constraints` 用 `scope` 区分两类来源：

- `per_paper`：某一份卷自身的约束，`detail.paper` 标明卷序号（从 0 起）；
- `cross_paper`：跨卷共同题上限（`kind` 为 `shared_questions`，`detail.papers` 标明卷对）。

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 1, "type": "essay", "count": 2}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [1, 2], "max_shared_questions": 0}}
  ]
}
```

上例表示：第 2 卷（`paper: 1`）的题型数量约束与卷 2/卷 3 的零重叠上限相互冲突（题库中大题不足）；放宽其中任一条（例如允许两卷共用 1 题，或调大题量）后即可组卷。

## 多候选卷组（`alternative_count` 1～5）

在任意单卷/多卷请求上增加 `alternative_count`（及可选 `min_question_changes`），即可在**同一冻结题库、同一组原始约束**下一次取回多个候选卷组。求解过程：

1. 第 1 个候选按既有择优规则（单卷：难度偏差 → 重复知识点 → 题号序列；多卷：难度偏差总和 → 跨卷重复题 → 题号序列）求出；
2. 此后每轮**只追加、从不放宽**两类差异约束后重新分层择优：
   - **逐卷差异**：新候选的第 `p` 卷与此前**每个**候选的第 `p` 卷至少有 `min_question_changes` 道题不同（对单卷任务即整张卷的差异）；
   - **禁止卷序重排**（多卷）：不存在任何卷排列能让新候选与旧候选逐卷重合——不能只把 A/B 卷换个顺序充当新候选；
3. 全部候选共用同一 `seed`、单线程确定性求解，候选顺序可复现；
4. 某一轮在全部原约束 + 累计差异约束下无解（或限时内无法判定）时立即停止，返回已找到的实际数量并置 `exhausted: true`，**不会为凑数放宽任何约束**。

### 请求示例（单卷任务取 3 个候选）

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·3 个候选卷",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "alternative_count": 3,
    "min_question_changes": 2,
    "seed": 42
  }'
```

多卷任务同理，与 `paper_count` / `max_shared_questions` 同时给出即可，例如
`"paper_count": 3, "max_shared_questions": 0, "alternative_count": 4, "min_question_changes": 3`：
返回的每个候选都是一个完整的 3 卷平行卷组，候选间 3 张对应卷两两至少差 3 道题。

### 多候选响应

单候选（缺省 `alternative_count: 1`）响应与旧版**完全一致**；多候选时结果放在 `alternatives` 数组中，按既有择优规则从 `rank: 1` 起排序，**每组都带原有全部统计字段**（单卷为 `selected_ids` / `average_difficulty` / `difficulty_deviation` / `type_counts` / `knowledge_point_counts` / `repeated_knowledge_points`；多卷另含 `papers` 与 `total_difficulty_deviation` / `total_shared_questions` / `shared_question_pairs`）：

```json
{
  "status": "feasible",
  "alternative_count": 3,
  "returned_count": 3,
  "min_question_changes": 2,
  "exhausted": false,
  "alternatives": [
    {
      "rank": 1,
      "selected_ids": [1, 4, 9, "/* … */"],
      "total_score": 150,
      "average_difficulty": 0.52,
      "difficulty_deviation": 0.0,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"函数": 3, "导数": 2},
      "repeated_knowledge_points": 1,
      "paper": [ /* 完整题目对象 */ ]
    },
    {
      "rank": 2,
      "selected_ids": ["/* 与 rank 1 至少 2 题不同…… */"],
      "total_score": 150,
      "average_difficulty": 0.51,
      "difficulty_deviation": 0.01,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_point_counts": {"/* …… */": 0},
      "repeated_knowledge_points": 1,
      "paper": [ /* …… */ ]
    },
    { "rank": 3 }
  ],
  "seed": 42
}
```

多卷任务的多候选响应顶层另有 `paper_count` 与 `max_shared_questions`，每个候选元素形如：

```json
{
  "rank": 2,
  "papers": [
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]},
    {"selected_ids": ["/* … */"], "total_score": 150, "paper": [ /* …… */ ]}
  ],
  "total_difficulty_deviation": 0.05,
  "total_shared_questions": 0,
  "shared_question_pairs": [
    {"papers": [0, 1], "count": 0, "question_ids": []}
  ]
}
```

字段说明：

- `alternative_count`：请求的候选数量；`returned_count`：实际返回数量；
- `exhausted`：候选是否已穷尽——`true` 表示在不放宽任何原约束的前提下无法再给出满足差异要求的候选（`returned_count < alternative_count` 时恒为 `true`）；
- `min_question_changes`：本次实际采用的逐卷最少差异题数（回显缺省值）；
- `alternatives[i].rank`：从 1 起的择优排名；排名仅依据既有择优规则，候选间的差异约束不参与排序。

> 多候选任务同样可以发起[定向改卷](#定向改卷post-apitaskstask_idrevise)：以排名第 1（择优最优）的候选为基准卷冻结；改卷任务始终只返回一个新卷组（差异最大化择优），不再产生多候选。

## 定向改卷（`POST /api/tasks/{task_id}/revise`）

在一份**已完成且可行**的组卷结果上按卷施加人工编辑：指定每份卷**必须保留**与**必须移除**的题号，服务在**联合模型中对全部卷重新求解**，得到满足编辑条件且尽量少改的新平行卷组。

- 源任务仅允许状态为 `done` 且结果为 `feasible`：任务不存在返回 `404`；未完成或不可行返回 `422`。源任务为多候选结果时，以 `rank: 1`（择优最优）候选的各卷作为改卷基准。
- 请求体逐卷给出编辑（卷序号 `paper` 从 0 起）：`retain_ids`（必须选入）、`remove_ids`（不得选入）。
  - 两者题号重叠、引用题库中不存在的题号、卷序号越界、同一卷重复指定，均返回 `422`。
- 创建后返回一个**新的异步任务**：源任务请求、题库快照与编辑条件一并冻结，**源任务保持不变**；新任务沿用源任务的 `seed`，也可再次作为源任务继续改卷。
- 求解顺序在原有三层择优之前新增第 0 层：

  1. **各卷保留源卷原题的总数最大化**（先满足全部原始约束与人工编辑，再尽量少改）；
  2. 各卷难度偏差总和（单卷为自身偏差）最小；
  3. 跨卷重复题总数（单卷为重复知识点数）最少；
  4. 各卷题号序列最小。

```bash
curl -X POST http://localhost:8000/api/tasks/42/revise \
  -H "Content-Type: application/json" \
  -d '{
    "name": "A/B 平行卷·人工微调",
    "edits": [
      {"paper": 0, "retain_ids": [9],            "remove_ids": [12, 13]},
      {"paper": 1, "retain_ids": [4, 7],         "remove_ids": []}
    ]
  }'
# -> {"task_id": 43, "status": "pending"}
```

字段说明：

- `name`：新任务名称，缺省沿用源任务名称。
- `edits`：非空数组，每份卷至多一条；未出现的卷序表示该卷无人工编辑条件（仍参与联合重求解与保留最大化）。

新任务通过 `GET /api/tasks/43` 查询，顶层与 `result` 均带 `source_task_id`（上例为 `42`）；可行时逐卷给出与源卷的差异：

- `retained_ids`：新卷选入、且源卷也有的题号（保留的原题）；
- `added_ids`：源卷没有、新卷新增的题号；
- `removed_ids`：源卷有、新卷被移走的题号（按题库规范顺序输出）。

多卷时差异位于 `papers[i]` 内：

```json
{
  "status": "feasible",
  "source_task_id": 42,
  "paper_count": 2,
  "papers": [
    {
      "selected_ids": [1, 4, 8, 9, "/* … */"],
      "retained_ids": [1, 4, 9],
      "added_ids": [8],
      "removed_ids": [12, 13],
      "total_score": 150
    },
    {
      "selected_ids": [4, 7, "/* … */"],
      "retained_ids": [7],
      "added_ids": [4],
      "removed_ids": ["/* … */"],
      "total_score": 150
    }
  ],
  "seed": 42
}
```

单卷改卷时差异字段与 `source_task_id` 直接位于结果顶层（不返回 `papers` 数组）：

```json
{
  "status": "feasible",
  "source_task_id": 7,
  "selected_ids": [1, 2, 5],
  "retained_ids": [1, 5],
  "added_ids": [2],
  "removed_ids": [3]
}
```

### 定向改卷无解

编辑条件与原有约束冲突时，`conflict_constraints` 同样返回逐项删减验证的删除极小冲突集，其中人工编辑条件为：

- `kind: "question_retain"`（必须保留）/ `"question_remove"`（必须移除）；
- `scope: "per_paper"`，`detail.paper` 为卷序号、`detail.question_id` 为题号、`detail.edit` 为“保留”/“移除”，可直接定位到“哪份卷的哪道题”。

```json
{
  "status": "infeasible",
  "source_task_id": 42,
  "message": "不存在满足全部约束及人工编辑条件的平行卷组",
  "conflict_constraints": [
    {"kind": "type", "scope": "per_paper",
     "detail": {"paper": 0, "type": "essay", "count": 4}},
    {"kind": "question_retain", "scope": "per_paper",
     "detail": {"paper": 0, "question_id": 21, "edit": "保留"}},
    {"kind": "shared_questions", "scope": "cross_paper",
     "detail": {"papers": [0, 1], "max_shared_questions": 0}}
  ]
}
```

## 盲审分配（`POST /api/tasks/{task_id}/review-plan`）

为一份**已完成且结果可行**的组卷任务生成盲审分配方案：把审阅人**联合分配**到该任务的全部卷（单卷任务即 1 份卷，多卷/多候选/模板/改卷任务按其冻结的卷数）。分配在**同一个 CP-SAT 联合模型**中求解并**同步返回**——不产生新任务、不写库；同一任务同一请求重复调用结果完全一致（可复现）。

- 源任务仅允许状态为 `done` 且结果为 `feasible`：任务不存在返回 `404`；未完成或结果不可行返回 `422`。
- 硬约束（全部必须满足，不满足即整体无解）：
  - 每卷恰好 `reviewers_per_paper` 名审阅人，**同卷审阅人不重复**；
  - **回避**：审阅人不得分配到其 `excluded_papers` 声明的卷；
  - **总容量**：每名审阅人跨卷合计分到的卷数不超过其 `capacity`。
- 择优顺序（字典序，逐层最优后固定再优化下一层）：

  1. **最大个人负载**（单人分到的卷数）最小；
  2. **各人负载相对平均值的绝对偏差总和**最小（平均负载 = 总分配数 ÷ 审阅人数）；
  3. **按卷号展平的审阅人编号序列**最小（逐卷把选中审阅人的规范排序编号列表按字典序最小化，唯一破缺对称性，保证可复现）。

### 请求示例

```bash
curl -X POST http://localhost:8000/api/tasks/42/review-plan \
  -H "Content-Type: application/json" \
  -d '{
    "reviewers": [
      {"reviewer_id": "R1", "capacity": 3, "excluded_papers": [0]},
      {"reviewer_id": "R2", "capacity": 2, "excluded_papers": []},
      {"reviewer_id": "R3", "capacity": 2, "excluded_papers": [2]},
      {"reviewer_id": "R4", "capacity": 3, "excluded_papers": []}
    ],
    "reviewers_per_paper": 2
  }'
```

字段说明：

- `reviewers`：非空数组，每个元素为一名审阅人：
  - `reviewer_id`：审阅人编号（数字或字符串），**同一请求内不可重复**（整数 `1` 与字符串 `"1"` 是两个不同的审阅人）；
  - `capacity`：可承接的最大卷数，**正整数**（非正容量返回 `422`）；
  - `excluded_papers`：不可审的卷序号（回避关系，从 `0` 起），缺省 `[]`；**越界卷号**（≥ 源任务卷数或为负）返回 `422`。
- `reviewers_per_paper`：每卷所需审阅人数，**正整数**。

### 可行结果

```json
{
  "task_id": 42,
  "status": "feasible",
  "paper_count": 3,
  "reviewers_per_paper": 2,
  "required_total": 6,
  "max_load": 2,
  "assignments": [
    {"paper": 0, "reviewer_ids": ["R2", "R3"]},
    {"paper": 1, "reviewer_ids": ["R1", "R2"]},
    {"paper": 2, "reviewer_ids": ["R1", "R4"]}
  ],
  "loads": [
    {"reviewer_id": "R1", "capacity": 3, "load": 2},
    {"reviewer_id": "R2", "capacity": 2, "load": 2},
    {"reviewer_id": "R3", "capacity": 2, "load": 1},
    {"reviewer_id": "R4", "capacity": 3, "load": 1}
  ]
}
```

- `assignments`：逐卷分配结果（按卷号升序，卷内审阅人按编号规范排序）；
- `loads`：各人实际负载（不超过其 `capacity`）；`max_load` 为第 1 择优目标的最优值；
- `required_total`：总分配数（`paper_count × reviewers_per_paper`）。

### 无解结果（不返回部分方案）

无解时**不返回任何分配**，仅给出诊断：各卷可用人力（未回避该卷的审阅人）与总容量缺口：

```json
{
  "task_id": 42,
  "status": "infeasible",
  "message": "不存在满足全部回避与容量约束的盲审分配方案",
  "paper_count": 3,
  "reviewers_per_paper": 2,
  "required_total": 6,
  "capacity_total": 2,
  "capacity_gap": 4,
  "papers": [
    {"paper": 0, "required": 2, "available_count": 2,
     "available_reviewers": ["A", "B"], "shortage": 0},
    {"paper": 1, "required": 2, "available_count": 2,
     "available_reviewers": ["A", "B"], "shortage": 0},
    {"paper": 2, "required": 2, "available_count": 1,
     "available_reviewers": ["A"], "shortage": 1}
  ]
}
```

- `capacity_total`：审阅人合计可承接卷数（每人按其未回避卷数截断后求和）；`capacity_gap = max(0, required_total - capacity_total)` 为总容量缺口；
- `papers[i]`：第 `i` 卷的所需人数、可用人数与可用审阅人名单，`shortage` 为该卷人数缺口——总容量充足但分布不均（回避过于集中）时，可据此定位问题卷。

## 考场卷别排布（`POST /api/tasks/{task_id}/seat-plan`）

为一份**已完成、结果可行且含 2～5 份平行卷**的组卷任务生成考场卷别排布：把各卷按其**印量**分配到不超过 6×6 的考场座位上，在严格满足各卷印量的前提下使**同卷别接触边数最少**（尽量打散同卷相邻）。排布在**同一个 CP-SAT 模型**中求解并**同步返回**——不产生新任务、不写库；同一任务同一请求重复调用结果完全一致（可复现）。

- 源任务仅允许状态为 `done`、结果为 `feasible` 且卷数为 2～5：任务不存在返回 `404`；未完成、无结果或结果不可行返回 `422`；单卷任务（`paper_count` 缺省或为 `1`）返回 `422`。
- 硬约束：
  - 每个**可用座位**恰好放置 1 份卷；不可用座位（缺考/隔离位等）始终为空、不参与排布；
  - 每份卷恰好放满其印量（允许某卷印量为 `0`，即该卷不出现在考场中）；
  - **印量总和必须等于可用座位数**（`rows × cols − 不可用座位数`），不等返回 `422`；印量条数必须与源任务卷数一致，否则 `422`。
- 接触边数：对每条**上下左右相邻**的座位对，两端放置**同一卷别**时计 1 条；两端卷别不同，或任一端为空位，均不计。
- 择优顺序（字典序，逐层最优后固定再优化下一层）：

  1. **同卷别接触边总数**最少；
  2. 同最优值时按**行优先、卷号升序**固定结果——把逐座卷号按行优先展平后的序列取字典序最小（第一个不同的座位上卷号尽可能小），唯一破缺对称性，保证可复现。

> **最优性保证（仅证明最优才返回可行）**：求解按“接触边最少 → 逐块字典序破平”分阶段进行，每阶段都有求解限时。只有当接触边总数目标取得 `OPTIMAL`（已证明全局最小）、且**每个行优先破平分块**都在固定此前各块的前提下证明本块最优时，才返回 `feasible`。任一阶段在限时内**只找到可行解而未证明最优**（或完全无法判定）时，返回 `error` 结构，**不携带 `grid` / `seats` / `contact_edges` 等任何部分排布**——因为仅可行的解可能接触边并非最少，而首个未完成破平分块之前沿用上一阶段的排布只对已固定分块最优，整段卷号序列不满足字典序规则。行列、不可用座位与各卷印量等回显字段在错误时照常返回。6×6 及以下的常规题通常在毫秒级完成全部证明；下列“限时场景”仅在求解器受限时出现。

### 调用示例

#### 成功场景

```bash
curl -X POST http://localhost:8000/api/tasks/42/seat-plan \
  -H "Content-Type: application/json" \
  -d '{
    "rows": 4,
    "cols": 4,
    "unavailable_seats": [
      {"row": 0, "col": 0},
      {"row": 3, "col": 3}
    ],
    "quantities": [7, 7]
  }'
```

字段说明：

- `rows` / `cols`：考场行数与列数，均为 **1～6** 的正整数；
- `unavailable_seats`：不可用座位列表（缺省 `[]` 表示全场可用），每项 `row` / `col` 均从 `0` 起、必须在考场界内且不可重复，排布时这些位置为空位；
- `quantities`：**各卷印量**数组，按下标对应卷号 `0,1,…,N-1`（与组卷结果中卷下标一致，`N` 必须等于源任务卷数 2～5）；每项为非负整数，**总和必须等于可用座位数**。

### 可行结果

```json
{
  "task_id": 42,
  "status": "feasible",
  "rows": 4,
  "cols": 4,
  "paper_count": 2,
  "quantities": [7, 7],
  "total_seats": 16,
  "available_seats": 14,
  "unavailable_seats": [
    {"row": 0, "col": 0},
    {"row": 3, "col": 3}
  ],
  "grid": [
    [null, 0, 1, 0],
    [1, 0, 1, 0],
    [0, 1, 0, 1],
    [1, 0, 1, null]
  ],
  "seats": [
    {"row": 0, "col": 1, "paper": 0},
    {"row": 0, "col": 2, "paper": 1}
  ],
  "empty_seats": [
    {"row": 0, "col": 0},
    {"row": 3, "col": 3}
  ],
  "contact_edges": 2
}
```

- `grid`：`rows × cols` 的逐座卷号二维表（按行优先）：可用座位为卷号（从 `0` 起），不可用座位为 `null`；
- `seats`：全部可用座位的分配明细（按行优先、列升序），每项含 `row` / `col` / `paper`；
- `empty_seats`：空位（不可用座位）坐标（行优先升序）；
- `contact_edges`：最终排布的同卷别接触边总数——在满足各卷印量前提下的**全局最小值**（第 1 择优目标）；
- `available_seats`：可用座位数（`total_seats − 不可用座位数`）；`quantities` / `unavailable_seats` 回显请求。

> 例如 3×3 全场可用、印量 `[5, 4]` 时可以做到完全棋盘式交错，`contact_edges` 为 `0`；受空位分割或印量失衡限制无法完全交错时（如上例），返回的接触边数已是所有可行排布中的最小值。

### 限时场景（`error`，不返回部分排布）

求解器为“接触边总数”与每个行优先破平分块分别设定了求解限时（单线程、固定种子）。若某一阶段限时内**只找到可行解却未证明最优**，或完全无法判定，接口同步返回 HTTP `200`、`status: "error"` 与诊断信息；响应**只含回显字段**（`rows` / `cols` / `paper_count` / `quantities` / `available_seats` / `unavailable_seats`）与 `error`，不含 `grid`、`seats`、`empty_seats`、`contact_edges`、`total_seats` 等任何部分排布，调用方可在稍后原样重试（请求与种子不变，结果可复现；一旦全部阶段证明最优即返回可行排布）：

```bash
curl -X POST http://localhost:8000/api/tasks/42/seat-plan \
  -H "Content-Type: application/json" \
  -d '{"rows": 6, "cols": 6, "quantities": [12, 12, 12]}'
```

接触边目标未能在限时内证明最优时：

```json
{
  "task_id": 42,
  "status": "error",
  "rows": 6,
  "cols": 6,
  "paper_count": 3,
  "quantities": [12, 12, 12],
  "available_seats": 36,
  "unavailable_seats": [],
  "error": "求解器在规定时间内无法判定接触边总数的最优值"
}
```

接触边已证明最少、但某个行优先破平分块未能在限时内证明字典序最优时（同样不返回排布）：

```json
{
  "task_id": 42,
  "status": "error",
  "rows": 6,
  "cols": 6,
  "paper_count": 3,
  "quantities": [12, 12, 12],
  "available_seats": 36,
  "unavailable_seats": [],
  "error": "求解器在规定时间内无法证明行优先卷号序列的字典序最优"
}
```

### 拒绝情形

- 印量总和不等于可用座位数、印量为负、不可用座位越界或重复、行列数超出 1～6、出现未知字段：均返回 `422`（Pydantic 校验明细）；
- 印量条数与源任务卷数不一致（如三卷任务只给 2 个印量）：`422` 并提示需按卷号 `0~N-1` 逐卷给出；
- 源任务不是 `done` + `feasible`，或源任务不是 2～5 份平行卷：`422`；任务不存在：`404`。

## 组卷模板（可版本化）

模板把**题库无关的约束配置**固化为可复用的组卷蓝本：`total_score`、`type_counts`、`knowledge_coverage`、`difficulty_range`、`target_difficulty`、`mutex_groups`、`paper_count`、`max_shared_questions`、`alternative_count`、`min_question_changes`（字段语义与 `/api/tasks` 完全一致）。模板**创建后不可修改**——服务不提供任何修改/删除接口；要调整约束，只能基于既有模板**创建新版本**。

- 模板不含 `questions` / `seed` / `name`：题库快照与种子在实例化时提供；互斥题组引用的题号是否存在，也在实例化时按当次题库快照校验。
- 每个模板属于一个版本谱系（`root_id` 标识）：首版本 `version=1`，携带 `base_template_id` 创建时在该谱系内递增版本号，`base_template_id` 记录直接派生来源。谱系内每个版本都是完整、独立、不可变的配置快照。
- 实例化时**实际生效配置**（模板配置 ⊕ 白名单 overrides，含缺省值落定）与 `template_id` / `template_version` 一并冻结进任务记录；任务查询会回显这两个字段，求解流程、结果结构与 `/api/tasks` 完全一致，模板任务同样可以继续[定向改卷](#定向改卷post-apitaskstask_idrevise)。

### 创建模板（`POST /api/templates`）

```bash
curl -X POST http://localhost:8000/api/templates \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考模板",
    "config": {
      "total_score": 150,
      "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
      "knowledge_coverage": {"函数": 2, "导数": 1},
      "difficulty_range": [0.45, 0.6],
      "target_difficulty": 0.52,
      "mutex_groups": [[12, 13]],
      "paper_count": 2,
      "max_shared_questions": 0
    }
  }'
```

响应（`201`）回显冻结后的完整配置（缺省字段一并落定）：

```json
{
  "template_id": 1,
  "version": 1,
  "name": "高三数学月考模板",
  "root_id": 1,
  "base_template_id": null,
  "config": {
    "total_score": 150,
    "type_counts": {"single_choice": 8, "fill_blank": 4, "essay": 4},
    "knowledge_coverage": {"函数": 2, "导数": 1},
    "difficulty_range": [0.45, 0.6],
    "target_difficulty": 0.52,
    "mutex_groups": [[12, 13]],
    "paper_count": 2,
    "max_shared_questions": 0,
    "alternative_count": 1,
    "min_question_changes": null
  },
  "created_at": "2026-09-21 12:00:00"
}
```

- `config` 校验规则与 `/api/tasks` 的约束字段一致（难度区间有序、数量为正整数、`max_shared_questions` 仅多卷可设等）；**未知字段一律 422**。
- 创建新版本：同一请求体加 `"base_template_id": 1`，新模板 `version=2`、`root_id=1`、`base_template_id=1`；`base_template_id` 不存在返回 `404`。`config` 必须完整给出（版本间不做字段级合并）。
- `GET /api/templates/{template_id}` 返回同一结构；不存在返回 `404`。

### 实例化组卷任务（`POST /api/templates/{template_id}/tasks`）

```bash
curl -X POST http://localhost:8000/api/templates/1/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·9 月",
    "questions": [ /* ……本次题库快照…… */ ],
    "seed": 42,
    "overrides": {
      "difficulty_range": [0.4, 0.65],
      "knowledge_coverage": {"函数": 3, "导数": 1}
    }
  }'
# -> {"task_id": 57, "status": "pending", "template_id": 1, "template_version": 1}
```

- `questions`（必填）、`seed`、`name`、`use_quota`、`lease_seconds` 为实例化请求的独立字段；`name` 缺省沿用模板名称，`use_quota` / `lease_seconds` 语义与 `/api/tasks` 一致（见[题目曝光配额](#题目曝光配额use_quota)与[预留租约](#预留租约lease_seconds)）。
- `overrides` 逐字段覆盖模板配置，**仅允许白名单字段**：`total_score`、`type_counts`、`knowledge_coverage`、`difficulty_range`、`target_difficulty`、`mutex_groups`、`paper_count`、`max_shared_questions`、`alternative_count`、`min_question_changes`；显式置 `null` 表示清除模板值、回到缺省行为（如 `"target_difficulty": null` 恢复为区间中点）。
- 白名单之外的任何字段（未知字段、拼写错误、`questions`/`seed`/`name` 等）一律 `422`，错误明细列出全部可覆盖字段。
- 合并后的生效配置连同题库快照按 `/api/tasks` 的同一套规则全量校验，**非法合并**（如把 `paper_count` 覆盖为 `1` 而模板带有 `max_shared_questions`、难度区间倒置、互斥组引用了快照中不存在的题号等）返回 `422` 与逐字段错误明细。
- 校验通过但约束本身无解时，任务正常创建，结果按既有规则返回 `infeasible` 与最小冲突约束集。

实例化任务的查询与 `/api/tasks` 完全同构，`GET /api/tasks/57` 额外回显模板溯源信息：

```json
{
  "task_id": 57,
  "status": "done",
  "name": "高三数学月考·9 月",
  "seed": 42,
  "template_id": 1,
  "template_version": 1,
  "result": {"status": "feasible", "paper_count": 2, "papers": [ /* …… */ ]}
}
```

> 直接通过 `POST /api/tasks` 创建的任务不返回 `template_id` / `template_version` 字段，原有行为完全不变。

## 题目曝光配额（`use_quota`）

按题号控制题目可被组卷使用的次数：先通过 `PUT /api/quotas` 为题号设置可用次数，创建任务时声明 `use_quota: true`，任务完成**可行组卷**时即在**单个 SQLite 事务**中按题号**原子扣减**配额。

- **扣减口径**：以排名第 1 的候选卷组为准（单候选即结果本身），按各卷选中题号的**出现次数**扣减——多卷任务中同一题被多份卷选用时按次数累计（每份卷都是一次实际曝光）。
- **串行化校验**：校验-扣减在进程内互斥锁 + `BEGIN IMMEDIATE` 事务中串行执行，并发任务不会超扣；任一题余额不足（**未设置过配额的题按余额 0 计**）则**整组不扣减**，任务结果返回 `quota_exceeded` 及逐题缺额明细。
- **失败/无解不消耗**：结果为 `infeasible` 或 `error` 的任务不扣减任何配额；定向改卷（revise）任务不参与配额扣减。
- **查询回显**：`GET /api/tasks/{task_id}` 对启用配额的任务返回 `quota` 块，含本次预留（`reserved`）及扣减后的剩余次数快照（`remaining`）；未声明 `use_quota` 的任务不返回该字段。

### 设置 / 查询配额（`PUT` / `GET /api/quotas`）

```bash
curl -X PUT http://localhost:8000/api/quotas \
  -H "Content-Type: application/json" \
  -d '{
    "quotas": [
      {"question_id": 1,    "quota": 2},
      {"question_id": 2,    "quota": 2},
      {"question_id": 5,    "quota": 2},
      {"question_id": "A-1", "quota": 10}
    ]
  }'
# -> [{"question_id": 1, "quota": 2, "updated_at": "2026-09-22 12:00:00"}, ...]

curl http://localhost:8000/api/quotas
```

- `quota` 为**绝对值**（覆盖旧值），`0` 表示该题不可用；同一请求内题号不可重复，负值返回 `422`。
- 题号类型（数字/字符串）需与题库快照中的 `id` 一致，整数 `1` 与字符串 `"1"` 是两个不同的题号。

### 创建启用配额的任务

在普通请求体上加 `"use_quota": true`（模板实例化任务同样支持该字段，为实例化请求的独立字段、不属于模板配置，也不可经 `overrides` 覆盖）：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "knowledge_coverage": {"函数": 1},
    "difficulty_range": [0.3, 0.7],
    "target_difficulty": 0.5,
    "mutex_groups": [[2, 4]],
    "use_quota": true,
    "seed": 42
  }'
# -> {"task_id": 21, "status": "pending"}
```

配额充足时任务正常完成，`GET /api/tasks/21` 返回本次预留及剩余次数：

```json
{
  "task_id": 21,
  "status": "done",
  "quota": {
    "status": "reserved",
    "reservations": [
      {"question_id": 1, "reserved": 1, "remaining": 1},
      {"question_id": 2, "reserved": 1, "remaining": 1},
      {"question_id": 5, "reserved": 1, "remaining": 1}
    ]
  },
  "result": {"status": "feasible", "selected_ids": [1, 2, 5], "total_score": 20}
}
```

### 配额不足（`quota_exceeded`）

任一题余额不足时**整组不扣减**，任务仍以 `done` 完成，但结果为 `quota_exceeded` 并附缺额明细（`required` 本次需扣次数、`available` 扣前余额、`shortage` 缺额），可行卷不返回：

```json
{
  "task_id": 22,
  "status": "done",
  "quota": {"status": "exceeded", "reservations": []},
  "result": {
    "status": "quota_exceeded",
    "message": "题目曝光配额不足，本次组卷未扣减任何配额",
    "shortages": [
      {"question_id": 1, "required": 1, "available": 0, "shortage": 1}
    ]
  }
}
```

补足配额（重新 `PUT /api/quotas`）后重新提交任务即可。

### 预留租约（`lease_seconds`）

默认情况下配额一旦扣减即永久生效。创建启用配额的任务时可附 `lease_seconds`（正整数秒，模板实例化任务同样支持，为实例化请求的独立字段、不可经 `overrides` 覆盖；仅可与 `use_quota: true` 同时给出，否则 `422`），为本次预留建立**可过期的租约**：

- **建约即发 token**：任务创建响应立即返回一次性 `reservation_token` 与 `lease_seconds`，此时租约状态为 `awaiting`（任务尚未结算，到期时间未定）；token 仅此一次返回，查询接口不会再次暴露。
- **结算激活**：任务可行且配额扣减成功时，租约在扣减的**同一 SQLite 事务**中激活为 `reserved`，冻结到期时间 `expires_at = 结算时刻（UTC） + lease_seconds`；任务无解/失败或配额不足（`quota_exceeded`）时租约在同事务置为 `void`（作废，不占配额）。
- **主动确认**：任务完成后，持 token 在到期前调用 `POST /api/tasks/{task_id}/quota/confirm` 把租约置为 `confirmed`，配额转为永久扣减。
- **过期回收**：逾期未确认的预留不会自动消失，需调用 `POST /api/quotas/reclaim-expired`：在**单个 SQLite 事务**中原子地把所有 `reserved` 且已到期的租约标记为 `reclaimed`，并按 `quota_reservations` 把次数**逐题返还**到 `quotas`（可安全反复调用，通常由定时任务周期性触发）。
- **幂等与互斥**：确认与回收都走“状态 + 到期时间”双条件的条件更新（进程内互斥锁 + `BEGIN IMMEDIATE`），以行计数裁决——重复确认返回 `already_confirmed`、过期后确认返回 `code=expired` 且**绝不返还或重扣**、重复/并发回收只在首次返还，因此同一配额不可能被释放两次。

创建请求（普通任务/模板实例化均可）：

```bash
curl -X POST http://localhost:8000/api/tasks \
  -H "Content-Type: application/json" \
  -d '{
    "name": "高三数学月考·预留待确认",
    "questions": [ /* ……题库快照…… */ ],
    "total_score": 20,
    "type_counts": {"single_choice": 2, "essay": 1},
    "difficulty_range": [0.3, 0.7],
    "use_quota": true,
    "lease_seconds": 3600,
    "seed": 42
  }'
# -> {"task_id": 23, "status": "pending",
#     "lease_seconds": 3600,
#     "reservation_token": "k8Xl…（一次性，仅此返回）…"}
```

任务查询的 `quota.lease` 块实时返回租约状态与到期时间（`reserved` 但已过 `expires_at` 时报告 `expired`；实际返还仍只由回收接口完成）：

```json
{
  "task_id": 23,
  "status": "done",
  "quota": {
    "status": "reserved",
    "reservations": [{"question_id": 1, "reserved": 1, "remaining": 1}],
    "lease": {
      "status": "reserved",
      "lease_seconds": 3600,
      "expires_at": "2026-09-22T13:00:05Z",
      "confirmed_at": null,
      "released_at": null
    }
  }
}
```

主动确认：

```bash
curl -X POST http://localhost:8000/api/tasks/23/quota/confirm \
  -H "Content-Type: application/json" \
  -d '{"reservation_token": "k8Xl…"}'
# -> {"task_id": 23, "code": "confirmed",
#     "lease": {"status": "confirmed", "lease_seconds": 3600,
#               "expires_at": "2026-09-22T13:00:05Z",
#               "confirmed_at": "2026-09-22T12:05:10Z", "released_at": null}}
```

确认接口的状态码与 `code`：

| 情形 | HTTP | `code` / 说明 |
| --- | --- | --- |
| 到期前首次确认 | `200` | `confirmed` |
| 到期前再次确认 | `200` | `already_confirmed`（幂等） |
| 已过期 / 已被回收 | `422` | `expired`（配额仅由回收接口返还，确认不返还、不重扣） |
| 任务尚未结算 | `422` | `awaiting` |
| 任务未产生预留（无解/失败/配额不足） | `422` | `void` |
| token 错误 | `403` | — |
| 任务不存在 / 任务无租约 | `404` | — |

回收过期预留：

```bash
curl -X POST http://localhost:8000/api/quotas/reclaim-expired
# -> {"reclaimed_count": 1, "returned_total": 1,
#     "reclaimed": [{"task_id": 24, "question_id": 1, "returned": 1}]}
# 再次调用（或并发调用）：
# -> {"reclaimed_count": 0, "returned_total": 0, "reclaimed": []}
```

回收后任务查询的 `quota.lease.status` 为 `reclaimed` 且 `released_at` 为释放时刻；`GET /api/quotas` 中相应题号余额已加回本次预留次数。`quota.reservations` 与 `quota_settled` 审计事件保留扣减当时的快照不变（返还动作不改写历史）。

## 崩溃恢复与任务接管（`POST /api/tasks/recover`）

异步任务在 `POST /api/tasks` 等创建接口的**同一事务**中落库（`tasks` 行 + `created` 事件，带租约时另有 awaiting 租约行），随后才由后台 worker 领取执行——因此**服务退出（崩溃、被 kill、容器重启）不会丢失任务**，只会留下状态为 `pending` 或 `running` 的未决行。恢复机制把执行权从“进程内守护线程”改为“数据库领取租约”：

- **启动自动接管**：API 启动时立即扫描一轮，领取全部 `pending` 任务以及心跳为空或已陈旧的 `running` 任务，按记录中**冻结的请求、`seed` 与原任务号**重新求解——普通任务、模板实例化任务（冻结的 `template_id`/`version` 不回退）、定向改卷任务（冻结的 `source_request`/`source_selection`/`edits`）均覆盖。
- **运行中周期兜底**：守护线程每 `EXAM_TASK_SWEEP_SECONDS`（默认 `10` 秒）重扫一次，无需重启即可接管后崩溃的同伴；worker 执行期间每 5 秒写一次心跳（`tasks.heartbeat_at`），心跳在 `EXAM_TASK_STALE_SECONDS`（默认 `30` 秒）内的 `running` 任务视为仍有存活 worker，**不会**被抢占。
- **手动触发**：`POST /api/tasks/recover` 立即执行一轮扫描（幂等，可任意重复调用）。

领取在单个 `BEGIN IMMEDIATE` 事务内以**条件 UPDATE** 完成（`pending` 先到先得；`running` 仅在心跳为空或陈旧时可抢占；`done` 永不命中），条件更新的行计数是跨进程、多实例重复启动、与新提交并发时的唯一裁决——**同一任务同时只有一个领取者**。任务完成结算时再次校验持有令牌（`status='running' AND claimed_by=本worker`），未持有的结果写入连同配额扣减在同一事务整体回滚，因此**任务只有一个最终结果**；启用曝光配额的任务**只结算一次**（成功扣减/余额不足/作废的租约流转均与结果、审计事件同事务提交）。接管 `running` 任务不重复追加 `started` 事件（仅当链上确无该事件时补一次），恢复任务的审计链与正常任务同样闭合。

`done` 任务不参与任何接管扫描，因此**已完成任务与已冻结的成绩批次绝不重算**；冻结请求本身损坏等极端情况下，恢复仍会把任务收敛为可经 `GET /api/tasks/{task_id}` 查询的 `status=error` 结果（有 awaiting 租约时同步作废，不再永久悬置）。

### 调用示例

```bash
# 服务重启后无需任何操作；也可手动触发一轮接管扫描
curl -X POST http://localhost:8000/api/tasks/recover
# -> 200
# {"claimed": [12, 13],            # 本轮领取并入队恢复的未决任务号
#  "already_running": [14],        # 心跳仍新鲜、判定为存活 worker 执行中，本轮不介入
#  "inflight": [],                 # 已在本进程队列/执行中而跳过的任务号
#  "claimed_count": 2}

# 没有未决任务时（重复调用同样安全）：
curl -X POST http://localhost:8000/api/tasks/recover
# -> 200 {"claimed": [], "already_running": [], "inflight": [], "claimed_count": 0}

# 接管结果通过既有的任务查询接口轮询（请求/响应约定不变）：
curl http://localhost:8000/api/tasks/12
# -> {"task_id": 12, "status": "done", "result": {"status": "feasible", ...}, ...}

# 恢复执行异常时任务同样可查询，结果为 error：
# -> {"task_id": 13, "status": "done",
#     "result": {"status": "error", "error": "ValueError: ...", "traceback": "..."}}

# 审计链对恢复任务保持闭合（started 不重复、配额任务恒有 quota_settled）：
curl http://localhost:8000/api/tasks/12/audit
# -> {"task_id": 12, "valid": true, "events": [created, started, finished, ...], ...}
```

相关环境变量（均有默认值，Compose 无需修改）：

- `EXAM_TASK_STALE_SECONDS`：running 心跳陈旧阈值秒数，默认 `30`；心跳超过该时长未更新的任务才会被接管。
- `EXAM_TASK_SWEEP_SECONDS`：后台接管扫描周期秒数，默认 `10`；启动时总会额外立即扫描一轮。

> 多实例部署时让各实例共享同一 SQLite 文件即可：条件 UPDATE + `BEGIN IMMEDIATE` 保证跨实例领取互斥；新实例启动只接管无人持有的任务，不会打断其它实例正在执行的任务。

## 防篡改审计链

每个异步组卷任务都附带一条**只追加、不可变**的哈希链接审计链，落库于 `task_audit_events` 表。任务生命周期与配额结算的每个关键动作都在**对应业务写入所在的现有 SQLite 事务内**追加事件——业务改动与审计事件要么同时提交、要么同时回滚：

| 事件 `type` | 追加时机（同事务的业务写入） | 最小业务载荷 `payload` |
| --- | --- | --- |
| `created` | 插入 `tasks` 行（任务创建，恒为第 1 个事件） | `name`、`seed`、`request_sha256`（冻结请求文本的 SHA-256） |
| `started` | 状态 `pending → running` | `{"status": "running"}` |
| `finished` | 结果落库（`feasible` / `infeasible` / `error` / `quota_exceeded`） | `result_status`、`result_sha256`（结果文本的 SHA-256） |
| `quota_settled` | **配额扣减/判定不扣减/作废**（与扣减结果同一事务原子提交） | `outcome=reserved` 时为逐题 `items`（题号、`reserved` 扣减次数、`remaining` 扣后剩余快照）；`outcome=exceeded` 时为 `shortages` 缺额明细；`outcome=void` 时为 `{"result_status": "infeasible" 或 "error"}`（无解/失败/恢复异常未扣减，租约作废） |

普通任务的链为 `created → started → finished`（3 个事件）；声明 `use_quota` 的任务在 `finished` 后同事务追加 `quota_settled`（4 个事件，`reserved` / `exceeded` / `void` 三种结局都恰有一条）；任务尚未开始时只有 `created`。任务被崩溃恢复接管时 `started` 事件**不重复追加**（已存在则沿用；旧进程崩在写 `started` 之前则由接管者补一条），因此恢复后链仍为同样的 3/4 个事件。

### 哈希与规范化口径

- 事件序号 `seq`：同一任务内从 **1** 起连续递增；并发下事件追加与业务写入同受进程内互斥锁保护，同任务写入严格连续。
- 前驱哈希 `prev_hash`：等于链上前一事件的 `hash`；**首事件固定为 64 个 `0`**（创世哈希）。
- 事件哈希 `hash`：对如下事件体的**规范化 JSON**（UTF-8、对象键按字典序升序、无任何空白字符、非 ASCII 不转义）计算 SHA-256：

  ```
  {"task_id":…,"seq":…,"type":…,"created_at":…,"payload":{…},"prev_hash":"…"}
  ```

  事件体不含 `hash` 自身；`created_at` 为事件落库时刻的 UTC ISO 8601 时间（秒级，`Z` 结尾）。载荷中的 `request_sha256` / `result_sha256` 另把完整业务文本钉在链上，使链与 `tasks` 表形成交叉锚定。

### 查询审计链（`GET /api/tasks/{task_id}/audit`）

```bash
curl http://localhost:8000/api/tasks/21/audit
```

任务不存在返回 `404`。链完整时 `valid: true`，按序号返回全部事件与链尾锚点 `last_hash`：

```json
{
  "task_id": 21,
  "valid": true,
  "verified_at": "2026-09-22T12:00:05Z",
  "events": [
    {
      "seq": 1,
      "type": "created",
      "created_at": "2026-09-22T12:00:00Z",
      "payload": {
        "name": "高三数学月考",
        "seed": 42,
        "request_sha256": "57ac592d34ceb4d61403a8a3277b618335d35e71ad691d9d31aec568a…"
      },
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "hash": "cf867f54fbf0f295068395de27422de2d6929c97c3057d71e8bd8661302f9c84"
    },
    {"seq": 2, "type": "started", "created_at": "2026-09-22T12:00:00Z",
     "payload": {"status": "running"}, "prev_hash": "cf86…f9c84", "hash": "ed9b…0e802"},
    {"seq": 3, "type": "finished", "created_at": "2026-09-22T12:00:05Z",
     "payload": {"result_status": "feasible", "result_sha256": "9a79…b7dd"},
     "prev_hash": "ed9b…0e802", "hash": "458b…ac846"},
    {
      "seq": 4,
      "type": "quota_settled",
      "created_at": "2026-09-22T12:00:05Z",
      "payload": {
        "outcome": "reserved",
        "items": [
          {"question_id": 1, "reserved": 1, "remaining": 1},
          {"question_id": 2, "reserved": 1, "remaining": 0}
        ]
      },
      "prev_hash": "458b…ac846",
      "hash": "a301…7f21"
    }
  ],
  "break_point": null,
  "last_hash": "a301…7f21"
}
```

`outcome=exceeded`（配额不足、整组未扣减）时第 4 个事件形如：

```json
{
  "seq": 4,
  "type": "quota_settled",
  "payload": {
    "outcome": "exceeded",
    "shortages": [{"question_id": 2, "required": 1, "available": 0, "shortage": 1}]
  },
  "prev_hash": "…",
  "hash": "…"
}
```

`outcome=void`（任务无解、执行失败或恢复执行异常，未产生任何预留）时第 4 个事件形如 `{"outcome": "void", "result_status": "infeasible"}`（或 `"error"`）：不写任何 `quota_reservations` 记录，带租约时租约同步置为 `void`，声明 `use_quota` 的任务因此无论成败都有且仅有一条结算事件。

### 校验结论与首个断点

接口在每次请求时**重放整条链**，按下列顺序检查，命中第一个异常即停止并在 `break_point` 返回断点（`at_seq` 定位事件序号、`kind` 异常类别、`expected`/`actual` 期望与实际值），同时 `events` 仍返回链上全部记录便于排查：

1. **序号连续性**：空链为 `empty_chain`；事件必须从 `seq=1` 起逐条 +1，断号/跳号返回 `seq_gap`；首事件类型不是 `created` 返回 `unexpected_first_event`；
2. **前驱哈希**：`prev_hash` 不等于上一事件 `hash`（首事件不等于创世哈希）返回 `prev_hash_mismatch`；
3. **内容摘要**：按同一规范化口径重算事件体哈希，与存储 `hash` 不符返回 `hash_mismatch`；载荷不是合法 JSON / 非规范化文本返回 `payload_not_json` / `payload_not_canonical`；未知事件类型返回 `unknown_event_type`；
4. **生命周期**：`created` / `started` / `finished` 各至多一次（重复为 `duplicate_event`），且必须按 `created → started → finished → quota_settled` 的顺序出现（越序或缺前驱为 `event_order`）；
5. **与业务表交叉比对**：`created` 载荷的 `request_sha256` / `name` / `seed` 与 `tasks` 行不符（`request_digest_mismatch` / `name_mismatch` / `seed_mismatch`）；`finished` 的 `result_sha256` / `result_status` 与 `tasks.result` 不符（`result_digest_mismatch` / `result_status_mismatch`）；`quota_settled` 的逐题预留/缺额与 `quota_reservations`、任务结果不符（`reservations_mismatch` / `shortages_mismatch` / `settlement_outcome_mismatch`）；
6. **状态完备性**：`running`/`done` 任务缺 `started` 事件、`done` 任务缺 `finished` 事件、声明 `use_quota` 的任务缺 `quota_settled` 事件（分别为 `started_event_missing` / `finished_event_missing` / `quota_settled_event_missing`，此时 `at_seq` 为 `null`，断点位于链尾之后）。

校验在**单一只读事务快照**中读取任务行、事件链与预留明细后重放（不存在跨连接的瞬时不一致），任何一项失败 `valid` 即为 `false`，**不存在“链被改动但仍报告通过”的情况**——即使攻击者重新计算全部事件哈希使链自洽，业务文本摘要的交叉比对也会暴露篡改。例如下例中第 2 个事件的前驱哈希被改动：

```json
{
  "task_id": 21,
  "valid": false,
  "verified_at": "2026-09-22T12:03:00Z",
  "events": [ /* …链上全部事件仍原样返回… */ ],
  "break_point": {
    "at_seq": 2,
    "kind": "prev_hash_mismatch",
    "expected": "cf867f54fbf0f295068395de27422de2d6929c97c3057d71e8bd8661302f9c84",
    "actual": "0000000000000000000000000000000000000000000000000000000000000000"
  },
  "last_hash": "a301…7f21"
}
```

> 审计事件表只追加，服务不提供任何修改/删除接口；`last_hash` 可作为外部留存的链顶锚点，与历史响应中的值逐次比对即可发现库被离线改写。

## 录分与仲裁（初评/复评/仲裁）

教研组对**已完成且结果可行**（`status=done` 且 `result.status=feasible`）的任务录分；录分基准恒为任务结果中**排名第 1（择优最优）的候选卷组**——单候选即结果本身，多候选取 `alternatives` 中 `rank` 最小者（改卷任务溯源到其原始组卷请求的题库与分值）。每份卷的允许题目以该候选冻结的 `selected_ids` 为准，题分值取自任务冻结题库快照。

路径参数：

- `paper`：卷号（从 0 起，对应候选卷组的卷下标），越界返回 422；
- `examinee`：考生标识，整数或字符串均可。路径中以 JSON 编码区分类型：整数 `42` 直接写 `42`；字符串 `X7` 写 `%22X7%22`（即 `"X7"` 的 URL 编码）。**整数 42 与字符串 "42" 是两个不同考生**；
- `question`：题号，编码口径与考生标识相同，**整数 9 与字符串 "9" 是两道不同的题**；且必须是该卷 `selected_ids` 中的题，否则 422。

### 有效分与待处理原因

每题的两评分与有效分口径如下（查询逐题返回）：

| 状态 | 有效分 `effective_score` | 待处理原因 `pending_reason` |
| --- | --- | --- |
| 两轮都未录 | `null` | `no_scores`（等待两轮评分） |
| 仅有初评 | `null` | `missing_review`（等待补评） |
| 仅有复评 | `null` | `missing_initial`（等待补评） |
| 两评齐备且分差 ≤ 1 | 两评**均值**（差 0 为整数，差 1 为 `x.5`） | `null` |
| 两评齐备、分差 > 1、未仲裁 | `null` | `awaiting_arbitration` |
| 两评分差 > 1 且已有仲裁 | 仲裁分（整数） | `null` |

全卷**每题都有有效分**时响应才含 `total_score`（逐题有效分求和，可为半分）；任一题仍待处理则不返回该字段。

### 版本规则（乐观并发）

- 每题 `version` **从 0 起**；
- **首次录分**（该行尚不存在）与**补评**（此前缺初评或缺复评、填补空槽）**不需要 `version`**，写入后版本不变；
- 该轮分数已存在再提交即为**改分**，必须携带当前 `version`：成功后版本 **+1**，并在同一条 SQL 中**立即撤销旧仲裁**（`arbitration_score` 清空）；同分重复提交幂等（不递增、不撤仲裁）；
- **仲裁**属一次修改，必须携带当前 `version`，成功后 +1；同分重复提交幂等；
- `version` 缺失返回 **422**（`detail.code=version_required`）；版本与服务端不一致返回 **409**（`detail.code=version_conflict`）且**不写入任何内容**。并发的两个同版本改分恰有一个成功（条件 `UPDATE ... WHERE version=?` 的行计数裁决）。

### 录入/修改初评或复评（`PUT .../questions/{question}/scores`）

请求体：`initial_score` 或 `review_score` **恰好给一个**（0 至题目分值的整数），改分时附 `version`。未知字段、负数、超出该题分值、同时给两轮或一轮都不给均返回 422。

```bash
# 考生 42（整数标识），卷 0，题 1（5 分题）首次录初评 4 分（无需 version）
curl -X PUT http://localhost:8000/api/tasks/1/papers/0/examinees/42/questions/1/scores \
  -H "Content-Type: application/json" \
  -d '{"initial_score": 4}'

# 补复评 5 分（此前无复评，填空槽同样无需 version）；分差 1 -> 有效分 4.5
curl -X PUT http://localhost:8000/api/tasks/1/papers/0/examinees/42/questions/1/scores \
  -H "Content-Type: application/json" \
  -d '{"review_score": 5}'

# 改分（该轮已有分）：必须携带当前 version；成功后版本 +1、旧仲裁被撤销
curl -X PUT http://localhost:8000/api/tasks/1/papers/0/examinees/42/questions/1/scores \
  -H "Content-Type: application/json" \
  -d '{"initial_score": 3, "version": 0}'
# -> 409 {"detail":{"message":"版本不符：题目已被其他请求改动，未写入",
#                   "code":"version_conflict"}}   # 版本不符时
```

字符串标识的考生/题号需在路径中带引号编码：

```bash
# 考生 "X7"（字符串），字符串题号 "9"
curl -X PUT "http://localhost:8000/api/tasks/1/papers/0/examinees/%22X7%22/questions/%229%22/scores" \
  -H "Content-Type: application/json" -d '{"initial_score": 8}'
```

### 仲裁（`POST .../questions/{question}/arbitration`）

**仅限初评、复评齐备且相差超过 1 分**；仲裁分须为 0 至题目分值的整数，必须携带当前 `version`。

- 任一轮缺失：422，`detail.code=awaiting_review`（先补评）；
- 分差 ≤ 1：422，`detail.code=arbitration_not_needed`（均值已是有效分，不允许仲裁）；
- 仲裁分超出题目分值：422；
- 此后任何初评/复评改分都会**立即撤销**该仲裁；撤销后若新两评仍相差 >1，可凭新版本重新仲裁。

```bash
# 初评 9、复评 6（差 3），当前 version=0 -> 仲裁分 8
curl -X POST http://localhost:8000/api/tasks/1/papers/0/examinees/42/questions/3/arbitration \
  -H "Content-Type: application/json" \
  -d '{"score": 8, "version": 0}'
```

### 查询成绩单（`GET .../examinees/{examinee}/scores`）

在**单一只读事务快照**中返回该考生该卷的逐题状态（按首个候选卷组 `selected_ids` 顺序，未录分题同样列出）。响应中的空值字段（`initial_score` / `review_score` / `arbitration_score` / `effective_score` / `pending_reason` / `pending_message` / `total_score`）为 `null` 时省略。

```json
{
  "task_id": 1,
  "paper": 0,
  "examinee_id": 42,
  "questions": [
    {"question_id": 1, "initial_score": 4, "review_score": 5,
     "version": 0, "effective_score": 4.5},
    {"question_id": 2, "initial_score": 5, "review_score": 5,
     "version": 0, "effective_score": 5},
    {"question_id": 3, "initial_score": 9, "review_score": 6,
     "arbitration_score": 8, "version": 1, "effective_score": 8}
  ],
  "total_score": 17.5
}
```

录分结果持久化在 `paper_scores` 表（随 SQLite 数据卷保留，服务/容器重启不丢失）。任务不存在返回 404；任务未完成、无结果或结果不可行（`infeasible` / `error` / `quota_exceeded`）返回 422。

## 成绩批次发布（`PUT .../papers/{paper}/score-batches/{batch}`）

教研组把已完成可行任务**首个候选卷组**某卷上的一批考生成绩**按批次冻结发布**。请求给出任务号、卷号、批次标识与考生标识列表；服务端在**单个写事务（进程内互斥锁 + `BEGIN IMMEDIATE`）的同一数据库快照**内读取名单内每名考生该卷的全部录分行，逐题套用[有效分口径](#有效分与待处理原因)：

- **仅当名单中每名考生的该卷所有题目都有有效分**时，才一次性冻结**全批**逐题有效分与总分并原子提交（HTTP **201**）——校验与冻结共用同一事务快照，不存在“校验时有效、冻结前被改分”的中间态；
- **任一人缺评或待仲裁**：返回 HTTP **422**（`detail.code=scores_pending`），`blockers` 逐考生列出**第一道**未决题（`question_id` / `reason` / `message`，原因码与逐题成绩单一致），事务回滚，**整批不发布、不留批次痕迹**；补齐录分/仲裁后可凭**相同批次标识**重试。

**标识规则（整数与字符串严格区分）**：路径中的批次标识与请求体中的考生标识沿用录分的 JSON 编码口径——整数直接写（`7`、`1001`），字符串写 URL 编码的 JSON 串（`"B1"` → `%22B1%22`，`"X7"` → `%22X7%22`）。**整数 7 与字符串 "7" 是两个不同批次；整数 1001 与字符串 "1001" 是两名不同考生**。考生名单不能为空、不能含重复标识（布尔、浮点、数组等非 int/str 标识一律 422）。批次标识在**同一任务及卷号内唯一**。

**幂等、冲突与历史不可变**：

| 情形 | 响应 |
| --- | --- |
| 批次不存在且全员有效 | **201**，冻结并返回快照 |
| 同批次重试、名单**逐位置逐标识完全一致** | **200**，返回**原冻结快照**（`published_at` 等均为首次值） |
| 同批次重试但名单不一致（含仅顺序不同） | **409**，`detail.code=batch_conflict`，附既有名单 `existing_examinee_ids`，不写入 |
| 任一人缺评/待仲裁 | **422**，`detail.code=scores_pending` + `blockers`，整批回滚 |
| 发布后改分或仲裁 | **不改变任何历史批次**；用**新批次标识**发布才会读取最新成绩 |

> 重试一致性按请求名单判定：`[1001, 1002]` 与 `[1002, 1001]` 视为不同请求（409）；快照内逐考生结果始终按名单顺序返回。

### 发布请求示例

```bash
# 批次 B1（字符串标识，路径写 %22B1%22）发布考生 1001、1002 当前有效成绩
curl -X PUT "http://localhost:8000/api/tasks/1/papers/0/score-batches/%22B1%22" \
  -H "Content-Type: application/json" \
  -d '{"examinee_ids": [1001, 1002]}'
```

成功响应（201；只冻结逐题**有效分**与总分，有效分口径同 GET 成绩单）：

```json
{
  "task_id": 1,
  "paper": 0,
  "batch_id": "B1",
  "status": "frozen",
  "examinee_ids": [1001, 1002],
  "examinees": [
    {"examinee_id": 1001,
     "questions": [
       {"question_id": 1, "effective_score": 4.5},
       {"question_id": 2, "effective_score": 5},
       {"question_id": 3, "effective_score": 3}],
     "total_score": 12.5},
    {"examinee_id": 1002,
     "questions": [
       {"question_id": 1, "effective_score": 2},
       {"question_id": 2, "effective_score": 1.5},
       {"question_id": 3, "effective_score": 4}],
     "total_score": 7.5}
  ],
  "published_at": "2026-09-30T04:14:51Z"
}
```

有人未评完时整批拒绝（422，不产生批次）：

```json
{"detail": {
  "message": "名单内存在缺评或待仲裁的考生，整批未发布；补齐后可凭相同批次标识重试",
  "code": "scores_pending",
  "blockers": [
    {"examinee_id": 1002, "question_id": 3,
     "reason": "awaiting_arbitration",
     "message": "初评与复评分差超过 1 分，等待仲裁"}
  ]
}}
```

批次已存在但名单不一致（409）：

```json
{"detail": {
  "message": "批次已存在且本次请求名单与冻结名单不一致；历史批次不可修改，请改用新批次标识",
  "code": "batch_conflict",
  "existing_examinee_ids": [1001, 1002]
}}
```

### 按批次查询（`GET .../papers/{paper}/score-batches/{batch}`）

返回发布时刻冻结的名单、逐考生逐题有效分、总分与 `published_at`；发布之后的任何改分/仲裁都不影响结果。任务不存在返回 404；任务不可录分或卷号越界返回 422；任务、卷号有效但**批次不存在返回 404**（整数批次 `7` 与字符串批次 `"7"` 按编码分别查询）。

```bash
curl "http://localhost:8000/api/tasks/1/papers/0/score-batches/%22B1%22"
# -> 200 与首次冻结完全相同的快照；不存在 -> 404 {"detail": "该任务该卷下批次不存在"}
```

批次数据持久化在 `score_batches`（批次头：名单与冻结时刻）与 `score_batch_items`（逐考生逐题有效分/总分快照）表中，随 SQLite 数据卷保留；批次记录只追加、永不更新或删除。


## 可行结果

```json
{
  "status": "feasible",
  "selected_ids": [1, 2, 5],
  "total_score": 20,
  "average_difficulty": 0.525,
  "difficulty_deviation": 0.025,
  "type_counts": {"single_choice": 2, "essay": 1},
  "knowledge_point_counts": {"集合": 1, "函数": 1, "数列": 1},
  "repeated_knowledge_points": 0,
  "paper": [ /* 完整题目对象 */ ]
}
```

## 无解结果（最小冲突约束集）

当约束无解时，`status` 为 `infeasible`，`conflict_constraints` 给出经**逐项删减验证**的删除极小冲突集——即其中任何一条被放宽/删除后，其余约束即可满足：

```json
{
  "status": "infeasible",
  "message": "不存在满足全部约束的试卷",
  "conflict_constraints": [
    {"kind": "total_score",      "detail": {"total_score": 20}},
    {"kind": "difficulty_range", "detail": {"range": [0.9, 1.0]}}
  ]
}
```

单卷冲突项的 `kind` 取值：`total_score` / `type` / `coverage` / `difficulty_range` / `mutex`；多卷时另有跨卷项 `shared_questions`（`scope: "cross_paper"`），单卷项带 `scope: "per_paper"`。定向改卷任务的人工编辑条件为 `question_retain` / `question_remove`（均带 `scope: "per_paper"`，可定位到 `detail.paper` 与 `detail.question_id`）。
