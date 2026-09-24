"""续办案卷与渠道缓冲领域服务。

设计要点：
- 事实表（监管事件、材料版本、核查决定、签署、许可证事件、快照、订单标记、
  任务）全部只追加；许可证表上的可变指针（状态、证面有效期、唯一生效案卷）
  仅服务于实时并发判定，时点解释一律从许可证事件流重放，不依赖可变列。
- 渠道状态由 :mod:`pharmacy_identity.rules` 纯函数重算，每次重评向
  ``channel_snapshots`` 追加一行（与上一快照同态则不重复追加）。
- 所有时点查询按"记录时间"（recorded_at / signed_at / decided_at <= at）
  裁剪可见事实：晚到回执在被记录之前不可见、其影响只从记录时刻起生效，
  因此不倒改此前任何快照与决定。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    BufferSignoff,
    CaseVersion,
    ChannelSnapshot,
    License,
    LicenseEvent,
    MaintenanceTask,
    MaterialCheck,
    MaterialRevision,
    RegionRule,
    RegulatoryEvent,
    RenewalCase,
    OrderRiskMarker,
    as_utc,
    utcnow,
)
from .rules import (
    CHANNELS,
    CaseContext,
    ChannelDecision,
    EventFact,
    LicenseFact,
    MaterialFact,
    RuleFact,
    SignoffFact,
    evaluate,
)

REGULATORY_EVENT_TYPES = {"accepted", "correction", "rejected", "approved", "licensed"}
SIGNOFF_ROLES = {"compliance", "business"}
MATERIAL_VERIFY_RESULTS = {"verified", "rejected"}


class DomainError(Exception):
    """业务规则冲突；HTTP 层按 code 映射状态码。"""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class IdentityService:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------ 基础

    def register_license(
        self,
        *,
        license_no: str,
        region: str,
        operator_id: str,
        store_id: str,
        current_expiry: datetime,
    ) -> License:
        existing = self.session.scalar(
            select(License).where(
                License.license_no == license_no, License.region == region
            )
        )
        if existing:
            return existing
        expiry = as_utc(current_expiry)
        license_row = License(
            license_no=license_no,
            region=region,
            operator_id=operator_id,
            store_id=store_id,
            current_expiry=expiry,
            initial_expiry=expiry,
            status="active",
        )
        self.session.add(license_row)
        self.session.flush()
        self._reevaluate(license_row.id, at=utcnow(), trigger="license_registered")
        return license_row

    def register_rule(
        self,
        *,
        region: str,
        rule_version: str,
        effective_from: datetime,
        buffer_days: int = 0,
        online_buffer_days: int | None = None,
        delivery_buffer_days: int | None = None,
        buffer_requires_acceptance: bool = True,
        buffer_continues_in_correction: bool = True,
        required_documents: list[str] | None = None,
        detail: dict | None = None,
    ) -> RegionRule:
        existing = self.session.scalar(
            select(RegionRule).where(
                RegionRule.region == region, RegionRule.rule_version == rule_version
            )
        )
        if existing:
            raise DomainError("rule_exists", f"规则版本 {rule_version} 已存在", 409)
        rule = RegionRule(
            region=region,
            rule_version=rule_version,
            effective_from=as_utc(effective_from),
            buffer_days=buffer_days,
            online_buffer_days=online_buffer_days if online_buffer_days is not None else buffer_days,
            delivery_buffer_days=delivery_buffer_days if delivery_buffer_days is not None else buffer_days,
            buffer_requires_acceptance=buffer_requires_acceptance,
            buffer_continues_in_correction=buffer_continues_in_correction,
            detail={"required_documents": list(required_documents or []), **(detail or {})},
        )
        self.session.add(rule)
        self.session.flush()
        return rule

    def _get_license(self, license_no: str, region: str) -> License:
        row = self.session.scalar(
            select(License).where(
                License.license_no == license_no, License.region == region
            )
        )
        if row is None:
            raise DomainError("license_not_found", f"许可证 {license_no} 不存在", 404)
        return row

    def _get_case(self, case_ref: str) -> RenewalCase:
        row = self.session.scalar(
            select(RenewalCase).where(RenewalCase.case_ref == case_ref)
        )
        if row is None:
            raise DomainError("case_not_found", f"案卷 {case_ref} 不存在", 404)
        return row

    # ------------------------------------------------------------ 案卷与材料

    def open_case(
        self,
        *,
        license_no: str,
        region: str,
        operator_id: str,
        store_id: str,
        initiated_by: str,
        case_ref: str | None = None,
    ) -> RenewalCase:
        """门店按许可证与经营主体提交续办批次；争抢时唯一生效案卷先到先得。"""
        license_row = self._get_license(license_no, region)
        if license_row.status == "transferred":
            raise DomainError(
                "license_transferred", "许可证主体已转让，不能再开续办案卷", 409
            )
        if license_row.effective_case_id:
            holder = self.session.get(RenewalCase, license_row.effective_case_id)
            if holder and holder.status == "open":
                raise DomainError(
                    "case_conflict",
                    f"许可证已有唯一生效案卷 {holder.case_ref}，续办不得并行",
                    409,
                )
        case_ref = case_ref or f"RC-{secrets.token_hex(5)}"
        if self.session.scalar(
            select(RenewalCase.id).where(RenewalCase.case_ref == case_ref)
        ):
            raise DomainError("case_ref_exists", f"案卷号 {case_ref} 已存在", 409)

        case = RenewalCase(
            case_ref=case_ref,
            license_id=license_row.id,
            license_no=license_no,
            region=region,
            operator_id=operator_id,
            store_id=store_id,
            initiated_by=initiated_by,
            status="open",
            current_version=0,
        )
        self.session.add(case)
        self.session.flush()
        # 实时并发占位：同一许可证只允许一个生效案卷；历史指针仍以事件重放为准。
        license_row.effective_case_id = case.id
        license_row.version += 1
        self.session.flush()
        self._append_license_event(
            license_row, "case_opened", utcnow(), {"case_id": case.id}
        )
        self._reevaluate(license_row.id, at=utcnow(), trigger="case_opened")
        return case

    def submit_batch(
        self,
        case_ref: str,
        materials: list[dict],
        *,
        note: str = "",
        at: datetime | None = None,
    ) -> dict:
        """提交一批材料并形成案卷新版本。

        相同 document_key + content_hash 重送 -> 沿用原记录（reuse_of）；
        同一标识内容变化 -> 新版本 revision 并进入核查（verification_pending）。
        """
        at = as_utc(at or utcnow())
        case = self._get_case(case_ref)
        if case.status != "open":
            raise DomainError(
                "case_not_open", f"案卷 {case_ref} 已 {case.status}，不能再提交材料", 409
            )

        resolved: list[MaterialRevision] = []
        reused: list[str] = []
        new: list[str] = []
        changed: list[str] = []
        for item in materials:
            key = item["document_key"]
            content_hash = item.get("content_hash") or hashlib.sha256(
                item["source_summary"].encode("utf-8")
            ).hexdigest()
            valid_from = as_utc(item["valid_from"])
            valid_until = as_utc(item["valid_until"]) if item.get("valid_until") else None

            last_revision = self.session.scalar(
                select(MaterialRevision.revision)
                .where(
                    MaterialRevision.case_id == case.id,
                    MaterialRevision.document_key == key,
                )
                .order_by(MaterialRevision.revision.desc())
            )

            # 同哈希：本案卷或同一许可证的历次案卷中已有原记录 -> 沿用。
            same = self.session.scalar(
                select(MaterialRevision)
                .join(RenewalCase, RenewalCase.id == MaterialRevision.case_id)
                .where(
                    RenewalCase.license_id == case.license_id,
                    MaterialRevision.document_key == key,
                    MaterialRevision.content_hash == content_hash,
                )
                .order_by(MaterialRevision.revision)
            )
            if same is not None:
                reused.append(key)
                resolved.append(same)
                continue

            revision = (last_revision or 0) + 1
            is_change = last_revision is not None
            material = MaterialRevision(
                case_id=case.id,
                document_key=key,
                revision=revision,
                source_summary=item["source_summary"],
                content_hash=content_hash,
                valid_from=valid_from,
                valid_until=valid_until,
                received_at=at,
                # 首次入卷不预设疑点；同一标识内容变化才进入核查。
                verification="verification_pending" if is_change else "unverified",
            )
            self.session.add(material)
            self.session.flush()
            (changed if is_change else new).append(key)
            resolved.append(material)

        # 固化案卷版本：收录本批解析后的材料版本。
        seq = case.current_version + 1
        version = CaseVersion(
            case_id=case.id,
            seq=seq,
            created_at=at,
            material_revision_ids=[m.id for m in resolved],
            note=note,
        )
        self.session.add(version)
        case.current_version = seq
        self.session.flush()

        self._reevaluate(case.license_id, at=at, trigger="materials_submitted")
        return {
            "case_ref": case.case_ref,
            "version_seq": seq,
            "reused": reused,
            "new": new,
            "changed": changed,
            "new_or_changed": new + changed,
            "material_ids": [m.id for m in resolved],
        }

    def resolve_material_check(
        self,
        case_ref: str,
        *,
        result: str,
        decided_by: str,
        document_key: str | None = None,
        material_revision_id: str | None = None,
        at: datetime | None = None,
        comment: str = "",
    ) -> MaterialRevision:
        at = as_utc(at or utcnow())
        if result not in MATERIAL_VERIFY_RESULTS:
            raise DomainError("bad_check_result", f"核查结果 {result} 非法")
        case = self._get_case(case_ref)
        query = select(MaterialRevision).where(MaterialRevision.case_id == case.id)
        if material_revision_id:
            query = query.where(MaterialRevision.id == material_revision_id)
        else:
            if not document_key:
                raise DomainError("bad_request", "需要 document_key 或 material_revision_id")
            query = query.where(
                MaterialRevision.document_key == document_key
            ).order_by(MaterialRevision.revision.desc())
        material = self.session.scalars(query).first()
        if material is None:
            raise DomainError("material_not_found", "材料版本不存在", 404)

        self.session.add(
            MaterialCheck(
                material_revision_id=material.id,
                result=result,
                decided_by=decided_by,
                decided_at=at,
                comment=comment,
            )
        )
        material.verification = result
        material.verified_by = decided_by
        material.verified_at = at
        self.session.flush()
        self._reevaluate(case.license_id, at=at, trigger="material_checked")
        return material

    def _latest_check(self, material_id: str, at: datetime) -> MaterialCheck | None:
        return self.session.scalar(
            select(MaterialCheck)
            .where(
                MaterialCheck.material_revision_id == material_id,
                MaterialCheck.decided_at <= at,
            )
            .order_by(MaterialCheck.decided_at.desc())
        )

    # ------------------------------------------------------------ 监管事件

    def add_regulatory_event(
        self,
        case_ref: str,
        event_type: str,
        *,
        occurred_at: datetime,
        detail: dict | None = None,
        recorded_at: datetime | None = None,
    ) -> RegulatoryEvent:
        """监管受理/补正/驳回/批准/发证只能追加；迟到回执不倒改后续决定。"""
        if event_type not in REGULATORY_EVENT_TYPES:
            raise DomainError("bad_event_type", f"监管事件类型 {event_type} 非法")
        case = self._get_case(case_ref)
        occurred_at = as_utc(occurred_at)
        recorded_at = as_utc(recorded_at or utcnow())

        prev_max_occurred = self.session.scalar(
            select(RegulatoryEvent.occurred_at)
            .where(RegulatoryEvent.case_id == case.id)
            .order_by(RegulatoryEvent.occurred_at.desc())
        )
        is_late = prev_max_occurred is not None and occurred_at < prev_max_occurred
        if not is_late:
            allowed = {
                "open": REGULATORY_EVENT_TYPES,
                "approved": {"licensed"},
                "rejected": set(),
                "licensed": set(),
            }[case.status]
            if event_type not in allowed:
                raise DomainError(
                    "case_terminal",
                    f"案卷已 {case.status}，不能再记录 {event_type} 事件",
                    409,
                )
        seq = self._next_regulatory_seq(case.id)
        event = RegulatoryEvent(
            case_id=case.id,
            seq=seq,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            is_late=is_late,
            detail=detail or {},
        )
        self.session.add(event)
        self.session.flush()

        # 迟到回执只留痕：不重放终局副作用，避免倒改已作出的决定；
        # 重放时引擎按 occurred_at 排序，晚到的较早事件也不会越过后续决定。
        if not is_late:
            self._apply_event_side_effects(case, event)
        # 影响只从记录时刻起生效：晚到事件不会倒改此前的快照与决定。
        self._reevaluate(
            case.license_id,
            at=recorded_at,
            trigger=f"regulatory_{event_type}" + ("_late" if is_late else ""),
        )
        return event

    def _apply_event_side_effects(
        self, case: RenewalCase, event: RegulatoryEvent
    ) -> None:
        license_row = self.session.get(License, case.license_id)
        if event.event_type == "accepted":
            self._schedule_buffer_expiries(case, license_row, event)
        elif event.event_type == "correction":
            deadline = _dt(event.detail.get("deadline_at"))
            if deadline:
                self._upsert_task(
                    task_type="correction_deadline",
                    due=deadline,
                    license_id=license_row.id,
                    case_id=case.id,
                    dedupe_key=f"case:{case.id}:correction:{event.seq}",
                    payload={"event_id": event.id, "seq": event.seq},
                )
        elif event.event_type == "rejected":
            case.status = "rejected"
            # 指针保留指向已驳回案卷：驳回至重新开案之间的时点解释仍须
            # 说明"因驳回而关闭"；新案卷开立时由 case_opened 事件接管指针。
            license_row.version += 1
            self._cancel_pending_tasks(case.id)
        elif event.event_type == "approved":
            case.status = "approved"
        elif event.event_type == "licensed":
            case.status = "licensed"
            new_expiry = _dt(event.detail.get("new_expiry"))
            if new_expiry:
                license_row.current_expiry = new_expiry
            self._cancel_pending_tasks(case.id, keep=("notification",))
            self._append_license_event(
                license_row, "renewed", event.recorded_at,
                {"case_id": case.id, "new_expiry": event.detail.get("new_expiry")},
                recorded_at=event.recorded_at,
            )
            self._append_license_event(
                license_row, "case_effective", event.recorded_at,
                {"case_id": case.id},
                recorded_at=event.recorded_at,
            )

    def _schedule_buffer_expiries(
        self, case: RenewalCase, license_row: License, event: RegulatoryEvent
    ) -> None:
        rule = self._rule_row_at(case.region, event.recorded_at)
        days_by_channel = {
            "physical": rule.buffer_days if rule else 0,
            "online": rule.online_buffer_days if rule else 0,
            "delivery": rule.delivery_buffer_days if rule else 0,
        }
        for channel, days in days_by_channel.items():
            if days <= 0:
                continue
            due = license_row.current_expiry + timedelta(days=days)
            self._upsert_task(
                task_type="buffer_expiry",
                channel=channel,
                due=due,
                license_id=license_row.id,
                case_id=case.id,
                dedupe_key=f"case:{case.id}:buffer_expiry:{channel}",
                payload={"event_id": event.id, "buffer_days": days},
                replace_existing=True,
            )

    # ---------------------------------------------------------------- 双签

    def sign_buffer(
        self,
        case_ref: str,
        *,
        role: str,
        signer: str,
        channels: list[str] | None = None,
        at: datetime | None = None,
    ) -> BufferSignoff:
        """缓冲放行：合规与业务负责人基于同一案卷版本分别签署；发起人不能自批。"""
        at = as_utc(at or utcnow())
        if role not in SIGNOFF_ROLES:
            raise DomainError("bad_role", f"签署角色 {role} 非法")
        case = self._get_case(case_ref)
        if case.status not in ("open", "approved"):
            raise DomainError(
                "case_not_signable", f"案卷 {case.status} 状态不可签署缓冲放行", 409
            )
        if signer == case.initiated_by:
            raise DomainError(
                "self_approval_forbidden", "发起人不能自批缓冲放行", 403
            )
        channels = channels or list(CHANNELS)
        bad = [c for c in channels if c not in CHANNELS]
        if bad:
            raise DomainError("bad_channel", f"未知渠道：{bad}")
        version_seq = case.current_version
        if version_seq == 0:
            raise DomainError("no_case_version", "案卷尚无材料版本，不能签署", 409)
        existing = self.session.scalar(
            select(BufferSignoff).where(
                BufferSignoff.case_id == case.id,
                BufferSignoff.case_version_seq == version_seq,
                BufferSignoff.role == role,
            )
        )
        if existing:
            raise DomainError(
                "already_signed",
                f"{role} 已基于版本 {version_seq} 签署；案卷升版后须重新审阅签署",
                409,
            )
        signoff = BufferSignoff(
            case_id=case.id,
            case_version_seq=version_seq,
            role=role,
            signer=signer,
            signed_at=at,
            channels=channels,
        )
        self.session.add(signoff)
        self.session.flush()
        self._reevaluate(case.license_id, at=at, trigger=f"signoff_{role}")
        return signoff

    # ------------------------------------------------- 暂停 / 转让 / 订单标记

    def suspend_license(
        self,
        *,
        license_no: str,
        region: str,
        occurred_at: datetime | None = None,
        reason: str = "",
        completed_orders: dict[str, list[str]] | None = None,
    ) -> LicenseEvent:
        return self._license_lifecycle(
            license_no, region, "suspended", occurred_at, reason,
            completed_orders, "license_suspended_order_risk",
        )

    def resume_license(
        self, *, license_no: str, region: str, occurred_at: datetime | None = None,
        reason: str = "",
    ) -> LicenseEvent:
        return self._license_lifecycle(
            license_no, region, "resumed", occurred_at, reason, None, ""
        )

    def transfer_license(
        self,
        *,
        license_no: str,
        region: str,
        new_operator_id: str,
        occurred_at: datetime | None = None,
        reason: str = "",
        completed_orders: dict[str, list[str]] | None = None,
    ) -> LicenseEvent:
        event = self._license_lifecycle(
            license_no, region, "transferred", occurred_at, reason,
            completed_orders, "license_transferred_order_risk",
            extra_detail={"new_operator_id": new_operator_id},
        )
        license_row = self._get_license(license_no, region)
        license_row.operator_id = new_operator_id
        return event

    def _license_lifecycle(
        self, license_no, region, event_type, occurred_at, reason,
        completed_orders, risk_code, extra_detail: dict | None = None,
    ) -> LicenseEvent:
        at = as_utc(occurred_at or utcnow())
        license_row = self._get_license(license_no, region)
        event = LicenseEvent(
            license_id=license_row.id,
            seq=self._next_license_event_seq(license_row.id),
            event_type=event_type,
            occurred_at=at,
            recorded_at=utcnow(),
            detail={"reason": reason, **(extra_detail or {})},
        )
        self.session.add(event)
        live_status = {"suspended": "suspended", "resumed": "active", "transferred": "transferred"}
        license_row.status = live_status[event_type]
        license_row.version += 1

        # 主体转让：未完成案卷立即失效并取消其待办任务。指针保留指向该案卷，
        # 使转让后的时点解释仍能说明"因主体转让而关闭、当时案卷为何"；
        # 规则引擎对 transferred 许可证优先关闭，不依赖指针是否为空。
        if event_type == "transferred" and license_row.effective_case_id:
            case = self.session.get(RenewalCase, license_row.effective_case_id)
            if case and case.status == "open":
                case.status = "superseded"
            self._cancel_pending_tasks_for_license(license_row.id)
        self.session.flush()

        # 立即重评未完成渠道。
        self._reevaluate(
            license_row.id, at=event.recorded_at, trigger=f"license_{event_type}"
        )
        # 已经成交的订单：只追加风险标记，绝不回改订单。
        if completed_orders and risk_code:
            for channel, order_nos in completed_orders.items():
                for order_no in order_nos:
                    self._add_order_marker(
                        order_no, license_row.id, channel, risk_code,
                        event.recorded_at, f"{event_type}: {reason}",
                    )
        return event

    def tag_orders(
        self,
        *,
        license_no: str,
        region: str,
        orders: list[dict],
        risk_code: str,
        at: datetime | None = None,
    ) -> list[OrderRiskMarker]:
        """对外的订单风险标记入口：仅对已成交订单追加标记。"""
        at = as_utc(at or utcnow())
        license_row = self._get_license(license_no, region)
        markers = []
        for order in orders:
            marker = self._add_order_marker(
                order["order_no"], license_row.id, order["channel"],
                risk_code, at, order.get("detail", ""),
            )
            if marker is not None:
                markers.append(marker)
        return markers

    def _add_order_marker(
        self, order_no, license_id, channel, risk_code, valid_from, detail
    ) -> OrderRiskMarker | None:
        existing = self.session.scalar(
            select(OrderRiskMarker).where(
                OrderRiskMarker.order_no == order_no,
                OrderRiskMarker.license_id == license_id,
                OrderRiskMarker.risk_code == risk_code,
            )
        )
        if existing:
            return None
        marker = OrderRiskMarker(
            order_no=order_no,
            license_id=license_id,
            channel=channel,
            risk_code=risk_code,
            detail=detail,
            marked_at=utcnow(),
            valid_from=valid_from,
        )
        self.session.add(marker)
        self.session.flush()
        return marker

    # ------------------------------------------------------------ 任务 / 恢复

    def _upsert_task(
        self, *, task_type, due, license_id, case_id, dedupe_key, payload,
        channel: str = "", replace_existing: bool = False,
    ) -> MaintenanceTask:
        existing = self.session.scalar(
            select(MaintenanceTask).where(MaintenanceTask.dedupe_key == dedupe_key)
        )
        if existing is not None:
            if replace_existing and existing.status == "pending":
                existing.status = "cancelled"
                existing.processed_at = utcnow()
            else:
                return existing
        task = MaintenanceTask(
            license_id=license_id,
            case_id=case_id,
            task_type=task_type,
            channel=channel,
            due_at=due,
            status="pending",
            dedupe_key=dedupe_key,
            payload=payload,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def _cancel_pending_tasks(self, case_id: str, keep: tuple[str, ...] = ()) -> None:
        for task in self.session.scalars(
            select(MaintenanceTask).where(
                MaintenanceTask.case_id == case_id,
                MaintenanceTask.status == "pending",
            )
        ).all():
            if task.task_type in keep:
                continue
            task.status = "cancelled"
            task.processed_at = utcnow()

    def _cancel_pending_tasks_for_license(self, license_id: str) -> None:
        for task in self.session.scalars(
            select(MaintenanceTask).where(
                MaintenanceTask.license_id == license_id,
                MaintenanceTask.status == "pending",
            )
        ).all():
            task.status = "cancelled"
            task.processed_at = utcnow()

    def run_due_tasks(self, now: datetime | None = None) -> list[dict]:
        """停服恢复后继续补正期限、缓冲到期与通知任务（墙钟时点不变）。"""
        now = as_utc(now or utcnow())
        due = self.session.scalars(
            select(MaintenanceTask)
            .where(
                MaintenanceTask.status == "pending",
                MaintenanceTask.due_at <= now,
            )
            .order_by(MaintenanceTask.due_at)
        ).all()
        results: list[dict] = []
        for task in due:
            notifications: list[dict] = []
            if task.task_type in ("buffer_expiry", "correction_deadline"):
                decisions = self._reevaluate(
                    task.license_id,
                    at=task.due_at,
                    decided_at=now,
                    trigger="catch_up_" + task.task_type,
                )
                affected = [
                    d for d in decisions
                    if d.channel == task.channel or not task.channel
                ]
                closed = [d for d in affected if d.state == "closed"]
                if closed:
                    notifications.append(
                        {
                            "type": task.task_type,
                            "channels": [d.channel for d in closed],
                            "license_id": task.license_id,
                            "due_at": task.due_at.isoformat(),
                        }
                    )
            task.status = "done"
            task.processed_at = now
            results.append(
                {
                    "task_type": task.task_type,
                    "channel": task.channel,
                    "due_at": task.due_at.isoformat(),
                    "notifications": notifications,
                }
            )
        self.session.flush()
        return results

    # ------------------------------------------------------------ 状态与解释

    def status_at(self, *, license_no: str, region: str, at: datetime) -> dict:
        """查询任一日期：渠道为何开放/受限/关闭，采用了哪些材料与规则。"""
        at = as_utc(at)
        license_row = self._get_license(license_no, region)
        state = self._license_state_at(license_row, at)
        rule_row = self._rule_row_at(region, at)
        rule = self._rule_fact(rule_row)
        case = (
            self.session.get(RenewalCase, state.effective_case_id)
            if state.effective_case_id
            else None
        )
        case_ctx = self._case_context_at(case, at, state) if case else None
        license_fact = LicenseFact(
            status=state.status, current_expiry=state.current_expiry
        )
        decisions = evaluate(license_fact, rule, case_ctx, at)
        return {
            "license_no": license_no,
            "region": region,
            "as_of": at.isoformat(),
            "license_status": state.status,
            "rule": self._rule_view(rule_row),
            "case": self._case_view(case, case_ctx),
            "channels": [self._decision_view(d) for d in decisions],
        }

    def _decision_view(self, decision: ChannelDecision) -> dict:
        materials = []
        for material_id in decision.material_ids:
            row = self.session.get(MaterialRevision, material_id)
            if row:
                materials.append(
                    {
                        "id": row.id,
                        "document_key": row.document_key,
                        "revision": row.revision,
                        "source_summary": row.source_summary,
                        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
                    }
                )
        events = []
        for event_id in decision.event_ids:
            row = self.session.get(RegulatoryEvent, event_id)
            if row:
                events.append(
                    {
                        "id": row.id,
                        "seq": row.seq,
                        "event_type": row.event_type,
                        "occurred_at": row.occurred_at.isoformat(),
                        "is_late": row.is_late,
                    }
                )
        return {
            "channel": decision.channel,
            "state": decision.state,
            "reason_code": decision.reason_code,
            "reason_detail": decision.reason_detail,
            "valid_from": decision.valid_from.isoformat() if decision.valid_from else None,
            "rule_version": decision.rule_version,
            "case_version_seq": decision.case_version_seq,
            "materials_used": materials,
            "events_used": events,
        }

    def _rule_view(self, rule: RegionRule | None) -> dict | None:
        if rule is None:
            return None
        return {
            "id": rule.id,
            "rule_version": rule.rule_version,
            "effective_from": rule.effective_from.isoformat(),
            "buffer_days": rule.buffer_days,
            "online_buffer_days": rule.online_buffer_days,
            "delivery_buffer_days": rule.delivery_buffer_days,
        }

    def _case_view(self, case: RenewalCase | None, ctx: CaseContext | None) -> dict | None:
        if case is None:
            return None
        return {
            "case_ref": case.case_ref,
            "version_seq": ctx.version_seq if ctx else None,
            "status": ctx.case_status if ctx else None,
            "initiated_by": case.initiated_by,
        }

    # ------------------------------------------------------- 追加事实的重放

    def _license_events_at(self, license_row: License, at: datetime) -> list[LicenseEvent]:
        return list(
            self.session.scalars(
                select(LicenseEvent)
                .where(
                    LicenseEvent.license_id == license_row.id,
                    LicenseEvent.recorded_at <= at,
                )
                .order_by(LicenseEvent.recorded_at, LicenseEvent.seq)
            ).all()
        )

    def _license_state_at(self, license_row: License, at: datetime) -> "_LicenseState":
        """从只追加的许可证事件重放某日：状态、证面有效期、唯一生效案卷。"""
        status = "active"
        expiry = license_row.initial_expiry
        effective_case_id: str | None = None
        for event in self._license_events_at(license_row, at):
            if event.event_type == "suspended":
                status = "suspended"
            elif event.event_type == "resumed":
                status = "active"
            elif event.event_type == "transferred":
                status = "transferred"
            elif event.event_type == "renewed":
                new_expiry = _dt(event.detail.get("new_expiry"))
                if new_expiry:
                    expiry = new_expiry
            elif event.event_type == "case_opened":
                effective_case_id = event.detail.get("case_id")
            elif event.event_type == "case_closed":
                if effective_case_id == event.detail.get("case_id"):
                    effective_case_id = None
            elif event.event_type == "case_effective":
                effective_case_id = event.detail.get("case_id")
        return _LicenseState(
            status=status, current_expiry=expiry, effective_case_id=effective_case_id
        )

    def _rule_row_at(self, region: str, at: datetime) -> RegionRule | None:
        rows = self.session.scalars(
            select(RegionRule)
            .where(RegionRule.region == region, RegionRule.effective_from <= at)
            .order_by(RegionRule.effective_from.desc())
        ).all()
        if rows:
            return rows[0]
        # 规则尚未生效时回退到该地区最早版本，保证可解释性。
        return self.session.scalar(
            select(RegionRule)
            .where(RegionRule.region == region)
            .order_by(RegionRule.effective_from)
        )

    def _rule_fact(self, rule: RegionRule | None) -> RuleFact | None:
        if rule is None:
            return None
        required = tuple((rule.detail or {}).get("required_documents", []))
        return RuleFact(
            id=rule.id,
            rule_version=rule.rule_version,
            buffer_days=rule.buffer_days,
            online_buffer_days=rule.online_buffer_days,
            delivery_buffer_days=rule.delivery_buffer_days,
            buffer_requires_acceptance=rule.buffer_requires_acceptance,
            buffer_continues_in_correction=rule.buffer_continues_in_correction,
            required_documents=required,
            detail=rule.detail or {},
        )

    def _case_status_at(
        self, case: RenewalCase, events: list[RegulatoryEvent], license_status: str
    ) -> str:
        """案卷某日状态：以当日可见监管事件的终局类型判定。

        终局一旦可见即不可被晚到事件撤销（迟到的受理/补正不会把已驳回、
        已发证案卷倒改回 open）；主体转让视为 superseded。
        """
        if license_status == "transferred":
            return "superseded"
        visible_types = {e.event_type for e in events}
        for terminal in ("rejected", "licensed", "approved"):
            if terminal in visible_types:
                return terminal
        return "open"

    def _case_context_at(
        self, case: RenewalCase, at: datetime, state: "_LicenseState"
    ) -> CaseContext:
        version = self.session.scalar(
            select(CaseVersion)
            .where(CaseVersion.case_id == case.id, CaseVersion.created_at <= at)
            .order_by(CaseVersion.seq.desc())
        )
        # 材料闸门以案卷内截至该日、每个文档标识的最新"有效"版本为准（跨批次
        # 汇总）：后批未重送的文档仍沿用此前批次；valid_from 未到或 valid_until
        # 已过的版本当日不可用，按缺材料处理。
        latest_rows: dict[str, MaterialRevision] = {}
        for row in self.session.scalars(
            select(MaterialRevision).where(
                MaterialRevision.case_id == case.id,
                MaterialRevision.received_at <= at,
            )
        ).all():
            if row.valid_from is not None and at < row.valid_from:
                continue
            if row.valid_until is not None and at > row.valid_until:
                continue
            current = latest_rows.get(row.document_key)
            if current is None or row.revision > current.revision:
                latest_rows[row.document_key] = row
        materials = tuple(
            self._material_fact_at(m, at)
            for m in sorted(latest_rows.values(), key=lambda r: r.document_key)
        )

        events = list(
            self.session.scalars(
                select(RegulatoryEvent).where(
                    RegulatoryEvent.case_id == case.id,
                    RegulatoryEvent.recorded_at <= at,
                )
            ).all()
        )
        event_facts = tuple(self._event_fact(e) for e in events)

        signoff_query = select(BufferSignoff).where(
            BufferSignoff.case_id == case.id,
            BufferSignoff.signed_at <= at,
        )
        if version is not None:
            signoff_query = signoff_query.where(
                BufferSignoff.case_version_seq == version.seq
            )
        signoffs = tuple(
            SignoffFact(
                role=s.role,
                signer=s.signer,
                signed_at=s.signed_at,
                channels=tuple(s.channels or []),
            )
            for s in self.session.scalars(signoff_query).all()
        )

        return CaseContext(
            case_id=case.id,
            case_ref=case.case_ref,
            version_seq=version.seq if version else 0,
            initiated_by=case.initiated_by,
            case_status=self._case_status_at(case, events, state.status),
            materials=materials,
            events=event_facts,
            signoffs=signoffs,
        )

    def _event_fact(self, event: RegulatoryEvent) -> EventFact:
        detail = dict(event.detail or {})
        new_expiry = detail.get("new_expiry")
        if isinstance(new_expiry, str):
            detail["new_expiry"] = _dt(new_expiry)
        deadline = detail.get("deadline_at")
        if isinstance(deadline, str):
            detail["deadline_at"] = _dt(deadline)
        return EventFact(
            id=event.id,
            seq=event.seq,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            detail=detail,
            is_late=event.is_late,
        )

    def _material_fact_at(self, row: MaterialRevision, at: datetime) -> MaterialFact:
        check = self._latest_check(row.id, at)
        return MaterialFact(
            id=row.id,
            document_key=row.document_key,
            revision=row.revision,
            verification=check.result if check else row.verification,
            valid_from=row.valid_from,
            valid_until=row.valid_until,
            source_summary=row.source_summary,
            content_hash=row.content_hash,
        )

    # ------------------------------------------------------------ 重评 / 快照

    def _reevaluate(
        self,
        license_id: str,
        *,
        at: datetime,
        trigger: str,
        decided_at: datetime | None = None,
    ) -> list[ChannelDecision]:
        license_row = self.session.get(License, license_id)
        state = self._license_state_at(license_row, at)
        case = (
            self.session.get(RenewalCase, state.effective_case_id)
            if state.effective_case_id
            else None
        )
        rule = self._rule_fact(self._rule_row_at(license_row.region, at))
        ctx = self._case_context_at(case, at, state) if case else None
        license_fact = LicenseFact(
            status=state.status, current_expiry=state.current_expiry
        )
        decisions = evaluate(license_fact, rule, ctx, at)

        decided_at = decided_at or utcnow()
        for decision in decisions:
            last_snapshot = self.session.scalar(
                select(ChannelSnapshot)
                .where(
                    ChannelSnapshot.license_id == license_id,
                    ChannelSnapshot.channel == decision.channel,
                )
                .order_by(
                    ChannelSnapshot.valid_from.desc(), ChannelSnapshot.decided_at.desc()
                )
            )
            if (
                last_snapshot
                and last_snapshot.state == decision.state
                and last_snapshot.reason_code == decision.reason_code
            ):
                continue
            self.session.add(
                ChannelSnapshot(
                    license_id=license_id,
                    channel=decision.channel,
                    state=decision.state,
                    reason_code=decision.reason_code,
                    reason_detail=decision.reason_detail,
                    rule_id=decision.rule_id,
                    rule_version=decision.rule_version,
                    case_id=decision.case_id,
                    case_version_seq=decision.case_version_seq,
                    material_ids=list(decision.material_ids),
                    event_ids=list(decision.event_ids),
                    valid_from=decision.valid_from,
                    decided_at=decided_at,
                    trigger=trigger,
                )
            )
        self.session.flush()
        return list(decisions)

    def _append_license_event(
        self,
        license_row: License,
        event_type: str,
        at: datetime,
        detail: dict,
        *,
        recorded_at: datetime | None = None,
    ) -> None:
        self.session.add(
            LicenseEvent(
                license_id=license_row.id,
                seq=self._next_license_event_seq(license_row.id),
                event_type=event_type,
                occurred_at=at,
                recorded_at=recorded_at or utcnow(),
                detail=detail,
            )
        )
        self.session.flush()

    def _next_license_event_seq(self, license_id: str) -> int:
        return (
            self.session.scalar(
                select(LicenseEvent.seq)
                .where(LicenseEvent.license_id == license_id)
                .order_by(LicenseEvent.seq.desc())
            )
            or 0
        ) + 1

    def _next_regulatory_seq(self, case_id: str) -> int:
        return (
            self.session.scalar(
                select(RegulatoryEvent.seq)
                .where(RegulatoryEvent.case_id == case_id)
                .order_by(RegulatoryEvent.seq.desc())
            )
            or 0
        ) + 1


def _dt(value) -> datetime | None:
    if value is None:
        return None
    return as_utc(value)


@dataclass
class _LicenseState:
    status: str
    current_expiry: datetime
    effective_case_id: str | None
