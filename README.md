# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款按层级可查。封账范围为**全局互斥的单一状态机**，全店只贯彻一种策略，不允许整店日封与机级批次混用。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 封账范围策略（`settings.close_strategy`，二选一）

### 1. 整店日封（`store`，默认）

- 一次日封覆盖该营业日**全部机具**有效流水，写入日级批次并锁定全日（实盘为整店现金）。
- 预览提供只读分机汇总，但**不产生机级 sealed 批**，分机维度只是报表，不是封账状态。
- 该策略下机封端点直接 409 拒绝，不存在孤立机封。

### 2. 分机先封（`till`）

- 每台机独立机封：写**机级批次 + 机级长短款**，只锁该机当日流水；机封进行中同机再导入/改/删流水一律 409，其他机具不受影响。
- 全店日封在**同一事务**内校验：
  1. 当日有流水的机具全部已机封（缺机则日封失败）；
  2. 当日零流水机具必须在 `waived_tills` 中显式收口（有流水的机具不得收口）；
  3. 日级系统合计（流水重算）= 各机已封批次 `system_total` 之和，不一致则失败，杜绝二次累加。
- 日封写日级批次 + 日级长短款（整店实盘口径），随后锁定全日。
- 零流水机具不能机封（无机可封），只能显式收口。

### 互斥与并发

- 所有写路径（流水增删改、机封、日封）均 `BEGIN IMMEDIATE`，SQLite 写事务天然串行；事务内重检日锁/机锁，日封与任一机封不可能并行写同一营业日。
- DB 层唯一索引兜底：日级批每营业日至多一条（`uq_day_batch`）、机级批每机每营业日至多一条（`uq_till_batch`）。
- 已存在任何 sealed 批次后，封账策略锁定不可切换，防止两种状态机在同一数据集混用。
- 崩溃残留的 `sealing` 批次在启动迁移时清理（不持锁、可重试）。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/close/preview` | 预览：日合计、只读分机汇总、机封状态、缺机/零流水机、合计可核性、`can_day_seal` |
| POST | `/api/close/seal` | 日封（按策略分派；till 策略可带 `waived_tills`） |
| POST | `/api/close/seal-till` | 分机先封策略下的单台机封（store 策略 409） |
| GET | `/api/close/batches` | 批次列表（含 `scope`/`till_code`/`lock_state`/锁粒度） |
| GET | `/api/close/variances` | 分层长短款（`scope=day/till`） |
| GET/PUT | `/api/settings/close-strategy` | 封账范围策略读写（sealed 后锁定） |

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```
