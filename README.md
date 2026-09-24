# 药房店铺身份服务

服务在店铺身份事实之上管理**许可证续办案卷**与**渠道缓冲放行**。Flask 负责 HTTP 边界，SQLAlchemy 连接 SQLite；默认数据库文件为 `data/pharmacy_identity.sqlite3`，可用 `DATABASE_PATH` 改址。

```bash
python -m pip install -e ".[test]"
python -m alembic upgrade head
pytest
flask --app 'pharmacy_identity:create_app()' run
```

容器启动先执行 Alembic 迁移，再起多进程 HTTP 服务。

## 领域边界

- **续办案卷（renewal cases）**：门店按"许可证 + 经营主体"提交续办批次。同一许可证同一时刻只有**唯一生效案卷**；案卷版本只增不改，每批材料固化为一个案卷版本。
- **材料版本**：材料带来源摘要、内容指纹与有效时点。相同文件（同 `document_key` + 同指纹）重送**沿用原记录**；同一标识内容变化产生新版本并**进入核查**，核查通过/驳回各自留痕。
- **监管事件**：受理 `accepted`、补正 `correction`、驳回 `rejected`、批准 `approved`、发证 `licensed` **只能追加**。事件带"发生时点 / 记录时点"；`occurred_at` 早于已记录事件的为**迟到事件**（`is_late=true`），只留痕、不重放终局副作用，影响只从记录时刻起生效，**不倒改此前决定**。
- **渠道临时状态**：实体店 `physical`、线上店 `online`、配送 `delivery` 分别按**地区规则版本**计算 `open / restricted / closed`，并给出理由码以及所采用的规则版本、案卷版本、材料与事件。
- **缓冲放行双签**：必须由合规 `compliance` 与业务负责人 `business` 基于**同一案卷版本**分别签署；**发起人不能自批**；案卷升版后旧签署失效，须重新签署。
- **暂停 / 主体转让**：许可证暂停或主体转让**立即重评**全部未完成渠道（关闭）；**已成交订单只追加风险标记**，不改订单。
- **停服恢复**：补正期限、各渠道缓冲到期、通知以墙钟时点登记任务；服务恢复后按原定时点补发，不重复执行。
- **时点查询**：`GET /channels/status?license_no=&region=&at=` 对任意日期重放追加事实，说明渠道为何开放、受限或关闭。

### 状态判定顺序（每个渠道）

1. 许可证暂停 / 主体转让 → 关闭（最高优先级）；
2. 已发证且在新证有效期内 → 开放；
3. 最新终局为驳回 → 关闭；批准待新证 → 受限；
4. 旧证仍有效 → 开放；
5. 越过旧证截止日后：截止前未受理 → 关闭；必备材料缺失/核查驳回/待核查 → 受限或关闭；逾补正期限 → 关闭；
6. 超过该渠道监管缓冲天数 → 关闭（到期日当天关闭）；
7. 缓冲窗口内缺双签 → 受限，双签齐备 → 开放。

## 主要接口

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /admin/licenses` | 登记许可证（证号、地区、主体、门店、旧证截止日） |
| `POST /admin/region-rules` | 登记地区规则版本（各渠道缓冲天数、是否要求受理、补正期是否续缓冲、必备材料） |
| `POST /renewal-cases` | 开立续办案卷（同许可证并行开案返回 409 `case_conflict`） |
| `POST /renewal-cases/{ref}/batches` | 提交材料批次，返回复用/新增/变更的材料与案卷版本号 |
| `POST /renewal-cases/{ref}/material-checks` | 材料核查结论 `verified` / `rejected` |
| `POST /renewal-cases/{ref}/regulatory-events` | 追加监管事件（含可选 `recorded_at` 模拟迟到回执） |
| `POST /renewal-cases/{ref}/signoffs` | 合规/业务双签（发起人自批返回 403） |
| `POST /licenses/suspensions` · `/resumptions` · `/transfers` | 暂停 / 恢复 / 主体转让，可附 `completed_orders` 追加风险标记 |
| `POST /orders/risk-markers` | 对已成交订单追加风险标记 |
| `POST /maintenance/run-due-tasks` | 停服恢复后补发到期任务与通知 |
| `GET /channels/status` | 任意日期的渠道状态与依据解释 |

## 编译检查

```bash
python3 -m compileall -q src
```
