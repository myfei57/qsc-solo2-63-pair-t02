# 改签后行李目标重算：架构决策记录

状态：已采纳并封存（首次改签时以 `policy.put` 落日志，并写入哈希链审计，审计 kind：`rebook_policy`）
封存策略名：`commit_point_cutoff`（按可拦回点截断，逐件判定）
被否决取向：`recall_all_then_reroute`（先把在途件全部拦回再重排）

## 1. 要解决的问题

旅客临时改签时，同一件行李可能处于三种物理位置：

1. 还在导入台（值机后、未进入分拣线）；
2. 已进入分拣线（在途，分流器尚未触发）；
3. 已经过分流点、正在落入或已经落入滑槽。

系统必须在改签落库后**自行**重算这件行李的目标，且在途判定、路由重算、分拣记录三者必须共用同一套事实；
控制台给出的结论必须能和审计留痕逐条对上；任何时候同一件行李不能有两条同时有效的目标航班；
任何重试（请求重试、进程崩溃后恢复）都不能让一件行李被分拣两次。

## 2. 两种取向与取舍（全部记录）

### 取向 A：先全部拦回，再统一重排（已否决）

- 时效：拦回动作需要停线/回流，整个批次被堵住，未改签的行李也被耽误，容易错过截载；
- 重复分拣：被拦回的行李整体重新上线，每件都发生第二次物理分拣（重复搬运、重复占用滑槽与人工）；
- 物理可行性：对已经过分流点或已经落入滑槽的行李，"拦回"在物理上不成立——这是该取向的硬伤，
  它隐含"每件在途都能回退"的错误假设；
- 实现面：需要批量回滚所有在途分拣记录并重建路由，回滚窗口长、与物理现场偏差大。

### 取向 B：按可拦回点截断，逐件判定（已采纳）

规则：只对"还来得及物理取消分流"的行李重定目标；拦不回来的就让它走完旧航程，
如实记录它最后落入哪条滑槽，并生成从旧滑槽到新航班的人工转运单。

- 时效：不停线、不回流，未改签行李零影响，新航班只接收真正赶得上的行李，截载风险最低；
- 重复分拣：拦回发生在分流器触发之前，取消的只是"已武装的分流计划（ALLOCATED）"，
  行李尚未物理落下，因此不构成第二次分拣；只有**一件行李最多一条有效分拣记录**；
- 物理诚实：承认存在"拦不回"区间，不承诺做不到的回退；代价是现场会看到两种结论
  （`recalled` 与 `let_ride`），且 `let_ride` 的行李需要人工转运；
- 被接受的成本：
  1. 过了拦回点的行李产生人工转运工单（进 recheck 队列，行李置 `diverted`）；
  2. 控制台/报表必须按两种结论分别归因，对账接口必须能把每个结论还原到审计；
  3. 拦回是两阶段（先问 PLC 能否取消、再提交），比直接改映射复杂，崩溃恢复必须处理
     "取消已发出、结论未提交"的悬空状态（见第 5 节）。

一句话取舍：**宁可让少数拦不回的行李带着准确记录走人工转运，也不停线、不重复分拣、不假装物理回退存在。**

## 3. 在途判定：唯一的几何事实来源

`bagsort/line/geometry.py` 是唯一掌握"滑槽在第几格、安全余量是多少"的地方：

- 每个滑槽字母对应一个分流格（A=10、B=20、C=30、D=40，线长 60，余量 2，均可配置）；
- `recall_point = 分流格 - 安全余量`；
- `position <= recall_point`：拦得回（`before_recall_point`）；
- `recall_point < position < 分流格`：拦不回（`past_recall_point`）；
- `position >= 分流格`：分流器已承诺（`diverter_committed`）；
- 即使取消来得及，还要校验新滑槽仍在行李下游（`new_chute_unreachable` 也算拦不回）。

在途判定（`LineTracker`）、路由重算（`ReroutePlanner`）、审计 detail 全部读取同一个几何结果，
不存在三套口径。位置来源有两种，且对皮带整体一致：确定性 `tick`（演示/测试）与
扫描器 `observe`（现场）；扫描器报告某件行李到了某格，表示皮带整体前进，其他在途件一起移动，
途中可能落槽。

## 4. 三种物理位置的处理结论

| 物理位置 | verdict | 动作 |
|---|---|---|
| 导入台 | `swapped_at_induction` | 同一事务内：旧目标 SUPERSEDED、新目标 ACTIVE、mapping 改写、bag.flight_id 改写；不产生分拣记录 |
| 在途且在拦回点前 | `recalled` | 两阶段取消旧分流（见第 5 节）；旧 ALLOCATED 记录回滚为 ROLLED_BACK；新目标生效；行李回到可重新上线状态 |
| 在途但过了拦回点 / 新滑槽不可达 / PLC 取消被拒 | `let_ride`（行程未完，订单保持 PREPARED） | 旧目标继续是唯一有效目标，行李落旧滑槽；落槽瞬间旧目标 DELIVERED、新目标 ACTIVE、生成人工转运单，订单记下落槽滑槽号 |
| 已落槽/已出线后才改签 | `let_ride`（立即结算） | 直接记录当前滑槽为落槽滑槽，旧目标 DELIVERED、新目标 ACTIVE、进人工转运 |

## 5. 两阶段拦回、崩溃恢复与幂等

拦回是 prepare/confirm 两段，避免"日志说取消了、物理上没取消"：

1. **prepare**：同一事务写入新的 PENDING 目标、把旧分流命令置 `cancel_pending`、订单置 `PREPARED`；
   此窗口 mapping 仍指向旧航班（旧目标仍 ACTIVE）；
2. 向物理网关 `post_cancel`；
3. **confirm（accepted）**：同一事务内命令置 `cancelled`、旧 ALLOCATED 分拣记录回滚、
   旧目标 SUPERSEDED、新目标 ACTIVE、mapping 改新航班、订单 `SETTLED/recalled`；
4. **confirm（rejected）**：命令恢复 `armed`（带 `cancel_rejected` 原因），订单转
   `SETTLED` 之前先保持"行程未完"的 `let_ride`，行李照常落旧滑槽，落槽时按第 4 节结算。

崩溃恢复：若进程在第 1 步与第 3 步之间消失，重放日志后该订单仍是 `PREPARED`，
`POST /api/rebooks/{id}/settle` 用同一命令 id 重发取消并完成；对已结算订单调用是幂等 no-op。

幂等键：

- 改签请求必须带 `request_id`，重复 `request_id` 直接返回原订单，不产生新目标/新订单；
- 分流命令 id 由 `(bag, 目标版本号)` 确定性生成（`div-<bag>-<n>`），重放与重试命中同一行；
- 目标 id 同理（`tgt-<bag>-<n>`），重放整条日志产生字节级一致的状态（有重放一致性测试）；
- 网关按 `(命令 id, 动作)` 记忆 ACK/NACK，arm 与 cancel 同命令 id 不会串结论。

## 6. "同一件行李不能有两条有效目标 / 不能走两遍"如何保证

单一有效目标：

- `state.mappings`（分拣/路由实际读取的投影）恒等于"ACTIVE 目标集合"，二者在同一事务写入；
- 新目标生效必须先把当前 ACTIVE 置 SUPERSEDED/DELIVERED；
- `GET /api/rebooks/integrity` 独立扫描并报告：每件行李 ACTIVE 目标数、mapping 与 ACTIVE 是否一致、
  mapping 是否在无 ACTIVE 时残留；有违例即 `holds=false`。

不重复分拣：

- 分拣记录有生命周期：`allocated`（已武装、可回滚）→ `landed`（物理事实，不可删）/
  `rolled_back`（拦回取消，永不物理发生）；
- `SortLog.was_executed` 只数未回滚的记录；拦回回滚的是 ALLOCATED，因此重新上线不算第二遍；
- 一件行李至多存在一条 live（armed/cancel_pending）分流命令，再武装直接冲突拒绝；
- 完整性报告独立复核"每件行李有效分拣记录数"与"live 分流命令数"。

回滚不破坏映射一致性：回滚（`sort.rollback`）、目标更替、mapping 改写、bag.flight_id 改写
在同一个提交事务内完成，提交前任何异常整笔丢弃（沿用 Store 的 pending/rollback 机制）。
审计链是只追加哈希链，回滚**不删除**任何历史行，只追加状态翻转。

## 7. 控制台结论与审计留痕如何对上

- 每次改签落一条 `RebookOrder`（含 phase、position、recall_point、divert_at、verdict、
  target_id、command_id、落槽滑槽、原因、request_id）；
- 同时向事件总线发布结论事件，审计订阅器写入哈希链：
  - `rebook_induction_swap`（stage=checkin）
  - `rebook_recall`（stage=sorter）
  - `rebook_let_ride`（stage=chute）
  - `rebook_policy`（stage=audit，首次封存）
  审计 detail 携带 old/new 航班、request_id、落槽滑槽、位置与拦回点；
- `GET /api/rebooks/{id}/reconcile` 仅依据审计条目反查该 request_id，
  与订单的 verdict、新旧航班逐条比对；`GET /api/rebooks/reconcile` 批量对账；
  控制台结论无法被审计复现即判失败，而不是展示层面的小毛病。

## 8. 主要落库命令（只追加日志）

`line.put`、`divert.put`、`target.put`、`rebook.put`、`rebook.index.put`、`policy.put`、
`mapping.remove`、`sort.rollback`、`sort.land`。全部可经 journal 重放重建，
快照持久化往返一致（有测试）。

## 9. 涉及模块

- `bagsort/line/`：几何（geometry）、物理网关（gateway）、分流命令状态机（controller）、
  位置/推进（tracker）、目标版本（targets）；
- `bagsort/rebook/`：策略封存（policy）、重算判定（planner）、编排与两阶段（service）、
  对账与不变量（reconcile）；
- 接入：`app.py`、`console/handlers.py`、`console/routes.py`；
- 测试：`tests/test_rebook.py`。
