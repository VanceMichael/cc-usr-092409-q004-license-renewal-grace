# 药房店铺身份服务

服务用于在连锁药房集中续办许可证期间，管理续办案卷并裁定实体店、线上店、配送三类渠道的临时资格。Flask 负责 HTTP 边界，SQLAlchemy 连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可使用 `DATABASE_PATH` 改址。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

代码按应用、数据库基础设施、表结构（`schema.py`）、规则引擎（`rules.py`）、领域服务（`service.py`）、迁移和测试分开。容器启动时先升级数据库，再启动多进程 HTTP 服务。

## 领域模型与核心不变量

- **续办案卷（dossier）**：门店按“许可证 + 经营主体”提交续办批次，一批一个案卷，以 `docket_no` 为幂等键。案卷每次材料或监管事件变化产生一个新 `revision`，签署绑定具体版本。
- **材料只追加、重送幂等**：每份材料带来源摘要、内容指纹与有效时点。同一 `doc_key` 且指纹相同的重送只更新重送次数并沿用原记录；同一标识内容变化则留版本历史并进入 `under_review` 核查，核查期间临时资格收紧。
- **监管事件只追加**：受理 `accepted`、补正 `correction_request`、回复 `answered`、驳回 `rejected`、批准 `approved`、发证 `license_issued` 只能追加。同一 `client_event_id` 的纸质回执重复送达只入账一次。已终结案卷不能再追加事件。
- **双时间线**：`occurred_at` 是文书载明时点（用于起算缓冲期/补正期限等绝对截止时间），`recorded_at` 是系统知悉时点。历史裁定只使用 `recorded_at <= 查询时点` 的事实，因此**迟到回执不会倒改此前的决定**，只在到达后产生新的渠道裁定行。
- **唯一生效案卷**：同一许可证任意时刻至多一个生效案卷（`dossier_effectiveness` 开放行由部分唯一索引约束）。先到先得，后来的争抢案卷照常留痕但不能签署放行；在位案卷驳回/撤回后，按提交顺序晋升下一个。
- **地区规则版本化**：缓冲天数、补正天数、各渠道在各案卷阶段的开放/受限/关闭状态存于 `rule_versions`，案卷提交时锁定版本，历史裁定永远可按同一版本复算。
- **缓冲放行双签**：除“旧证仍有效”和“新证已到”外，任何凭临时依据的开放都需要合规人员（`compliance`）与业务负责人（`business`）基于**同一案卷版本**分别签署；发起人不能自批，两个签署人必须不同。案卷进入新版本后旧签署失效。
- **暂停与主体转让**：许可证暂停或转让只追加 `license_actions`，立即重评该门店所有未完成渠道（全渠道关闭，理由分别为 `license_suspended` / `license_transferred`），恢复后按案卷状态重新裁定。
- **已成交订单只追加风险标记**：订单成交时冻结当时渠道状态作为下单依据；之后渠道收紧不回滚订单，只向其追加 `order_risk_flags`。
- **期限绝对、停服可续**：缓冲到期、补正期限、到期前通知都落为带绝对 `due_at` 的任务，不依赖进程内定时器。停服恢复后调用 `POST /tasks/run-due` 即一次性补齐错过的全部任务。
- **任意日期可解释**：`GET /stores/<code>/channels?as_of=...` 重放当时已知悉的事实，给出每个渠道的状态、阶段、理由、采用的规则版本、材料快照、事件清单与缓冲/补正截止时间。

渠道状态取值：`open`（开放）/ `restricted`（受限）/ `closed`（关闭）；渠道为 `physical` / `online` / `delivery`。

## HTTP API

基础数据（管理侧）：

| 方法 路径 | 说明 |
| --- | --- |
| `POST /admin/regions` | 登记地区 |
| `POST /admin/rule-versions` | 登记/更新地区规则版本 |
| `POST /admin/operators` | 登记经营主体 |
| `POST /admin/stores` | 登记门店 |
| `POST /admin/licenses` | 登记许可证（同时写入初始有效期） |

续办与裁定：

| 方法 路径 | 说明 |
| --- | --- |
| `POST /renewals/batches` | 提交续办批次（同 `docket_no` 重发幂等） |
| `POST /renewals/<docket>/materials` | 补送材料（相同文件沿用，内容变化进核查） |
| `POST /renewals/<docket>/materials/<doc_key>/review` | 材料核查结论 `accepted`/`rejected` |
| `POST /renewals/<docket>/events` | 追加监管事件（受理/补正/回复/驳回/批准/发证） |
| `POST /renewals/<docket>/signoffs` | 合规或业务对当前案卷版本签署 |
| `POST /licenses/<number>/actions` | 暂停/恢复/转让许可证 |
| `GET  /stores/<code>/channels?as_of=...` | 查询三渠道状态及完整裁定依据 |
| `POST /tasks/run-due` | 执行所有到期任务（停服恢复入口） |
| `GET  /tasks/pending` | 查看待执行期限任务 |
| `POST /orders` | 记录成交订单（冻结当时渠道状态） |
| `GET  /orders/<order_no>` | 查询订单快照与累计风险标记 |

## 时间约定

所有业务时点为朴素 UTC 字符串 `YYYY-MM-DDTHH:MM:SS`，字典序即时间序，SQLite 可直接比较。

## 编译检查

```bash
python3 -m compileall -q src
```
