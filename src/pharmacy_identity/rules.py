"""地区规则版本化的渠道状态引擎。

引擎是纯函数：给定某一时点 ``at`` 可见的许可证、案卷、材料、监管事件、
签署与当时生效的地区规则版本，分别计算实体店、线上店、配送三个渠道的
临时状态（open / restricted / closed）及理由，并回带所采用的材料、事件
与规则引用，供快照与时点解释使用。

可见性按"记录时间"（recorded_at / signed_at 等 <= at）裁剪：晚到的回执
在它真正进入系统之前不可见，因此不会倒改彼时已作出的决定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

Channel = Literal["physical", "online", "delivery"]
State = Literal["open", "restricted", "closed"]

CHANNELS: tuple[Channel, ...] = ("physical", "online", "delivery")
SIGNOFF_ROLES = ("compliance", "business")


@dataclass(frozen=True)
class MaterialFact:
    id: str
    document_key: str
    revision: int
    verification: str  # unverified | verification_pending | verified | rejected
    valid_from: object
    valid_until: object | None
    source_summary: str = ""
    content_hash: str = ""


@dataclass(frozen=True)
class EventFact:
    id: str
    seq: int
    event_type: str  # accepted | correction | rejected | approved | licensed
    occurred_at: object
    recorded_at: object
    detail: dict = field(default_factory=dict)
    is_late: bool = False


@dataclass(frozen=True)
class SignoffFact:
    role: str
    signer: str
    signed_at: object
    channels: tuple[str, ...]


@dataclass(frozen=True)
class LicenseFact:
    status: str  # active | suspended | transferred
    current_expiry: object


@dataclass(frozen=True)
class RuleFact:
    id: str
    rule_version: str
    buffer_days: int
    online_buffer_days: int
    delivery_buffer_days: int
    buffer_requires_acceptance: bool
    buffer_continues_in_correction: bool
    required_documents: tuple[str, ...] = ()
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CaseContext:
    case_id: str
    case_ref: str
    version_seq: int
    initiated_by: str
    case_status: str
    materials: tuple[MaterialFact, ...]
    events: tuple[EventFact, ...]
    signoffs: tuple[SignoffFact, ...]


@dataclass(frozen=True)
class ChannelDecision:
    channel: Channel
    state: State
    reason_code: str
    reason_detail: str
    rule_id: str | None
    rule_version: str | None
    case_id: str | None
    case_version_seq: int | None
    material_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    valid_from: object


def _visible(items, at, attr="recorded_at"):
    return tuple(i for i in items if getattr(i, attr) <= at)


def _events_by_type(events: tuple[EventFact, ...]) -> dict[str, EventFact]:
    """每种事件取 occurred_at 最晚的一条（同类型多轮时以最后一轮为准）。"""
    result: dict[str, EventFact] = {}
    for event in sorted(events, key=lambda e: (e.occurred_at, e.seq)):
        result[event.event_type] = event
    return result


def _latest_event(events: tuple[EventFact, ...]) -> EventFact | None:
    if not events:
        return None
    return sorted(events, key=lambda e: (e.occurred_at, e.seq))[-1]


def _channel_buffer_days(rule: RuleFact, channel: Channel) -> int:
    if channel == "physical":
        return rule.buffer_days
    if channel == "online":
        return rule.online_buffer_days or rule.buffer_days
    return rule.delivery_buffer_days or rule.buffer_days


def _material_gate(
    rule: RuleFact, materials: tuple[MaterialFact, ...]
) -> tuple[str | None, MaterialFact | None]:
    """返回 (阻塞原因, 相关材料)。required 文档以案卷版本中最新一版为准。"""
    latest: dict[str, MaterialFact] = {}
    for material in sorted(materials, key=lambda m: m.revision):
        latest[material.document_key] = material
    for key in rule.required_documents:
        material = latest.get(key)
        if material is None:
            return "required_material_missing", None
        if material.verification == "rejected":
            return "material_rejected", material
    for key in rule.required_documents:
        material = latest.get(key)
        if material is not None and material.verification in (
            "unverified",
            "verification_pending",
        ):
            return "material_verification_pending", material
    return None, None


def _valid_signoffs(
    case: CaseContext, channel: Channel
) -> tuple[set[str], tuple[SignoffFact, ...]]:
    """返回已满足的角色集合与有效签署记录。

    有效签署必须：基于案卷当前版本、签署人不是发起人、覆盖该渠道。
    """
    roles: set[str] = set()
    used: list[SignoffFact] = []
    for signoff in case.signoffs:
        if signoff.signer == case.initiated_by:
            continue
        if signoff.channels and channel not in signoff.channels:
            continue
        roles.add(signoff.role)
        used.append(signoff)
    return roles, tuple(used)


def evaluate_channel(
    channel: Channel,
    license_fact: LicenseFact,
    rule: RuleFact | None,
    case: CaseContext | None,
    at,
) -> ChannelDecision:
    event_ids: tuple[str, ...] = ()
    material_ids: tuple[str, ...] = tuple(m.id for m in case.materials) if case else ()
    rule_id = rule.id if rule else None
    rule_version = rule.rule_version if rule else None
    case_id = case.case_id if case else None
    version_seq = case.version_seq if case else None

    def decide(state, reason, detail="", valid_from=None, events=(), materials=None):
        return ChannelDecision(
            channel=channel,
            state=state,
            reason_code=reason,
            reason_detail=detail,
            rule_id=rule_id,
            rule_version=rule_version,
            case_id=case_id,
            case_version_seq=version_seq,
            material_ids=tuple(materials if materials is not None else material_ids),
            event_ids=tuple(events),
            valid_from=valid_from or at,
        )

    # 1. 许可证暂停 / 主体转让：立即关闭，优先级最高。
    if license_fact.status == "suspended":
        return decide("closed", "license_suspended", "许可证已暂停，渠道立即关闭")
    if license_fact.status == "transferred":
        return decide("closed", "license_transferred", "经营主体已转让，原主体渠道关闭")

    events = _visible(case.events, at) if case else ()
    events = tuple(sorted(events, key=lambda e: (e.occurred_at, e.seq)))
    event_ids = tuple(e.id for e in events)
    by_type = _events_by_type(events)
    latest = _latest_event(events)

    # 2. 已发证：新证覆盖期间开放。
    licensed = by_type.get("licensed")
    if licensed:
        new_expiry = licensed.detail.get("new_expiry")
        if new_expiry is None or at <= new_expiry:
            return decide(
                "open",
                "licensed_new_certificate",
                "新证已签发并在有效期内",
                events=(licensed.id,),
            )

    # 3. 驳回：案卷终结，关闭。
    if latest is not None and latest.event_type == "rejected":
        return decide(
            "closed",
            "renewal_rejected",
            latest.detail.get("reason", "续办申请被监管驳回"),
            events=(latest.id,),
        )

    # 4. 已批准但新证未到：受限营业。
    if latest is not None and latest.event_type == "approved":
        return decide(
            "restricted",
            "approved_awaiting_license",
            "续办已批准，新证尚未到达，暂按受限营业",
            events=(latest.id,),
        )

    # 5. 旧证仍在有效期：正常开放。
    if at <= license_fact.current_expiry:
        return decide(
            "open", "license_valid", "旧证仍在有效期内", valid_from=at, events=()
        )

    # —— 以下为越过旧证截止日之后的缓冲判定 ——
    if case is None or case.case_status in ("withdrawn", "superseded") or rule is None:
        return decide(
            "closed",
            "no_effective_renewal_case",
            "旧证到期且无唯一生效的续办案卷",
            events=event_ids,
        )

    accepted = by_type.get("accepted")
    if rule.buffer_requires_acceptance and accepted is None:
        return decide(
            "closed",
            "no_acceptance_before_expiry",
            "旧证到期前未取得监管受理，不得缓冲营业",
            events=event_ids,
        )

    # 6. 材料核查闸门：驳回关闭，待核查受限。
    gate_reason, gate_material = _material_gate(rule, case.materials)
    if gate_reason == "required_material_missing":
        return decide("restricted", gate_reason, "必备材料尚未入卷")
    if gate_reason == "material_rejected":
        return decide(
            "closed",
            gate_reason,
            f"材料 {gate_material.document_key} 核查未通过",
            materials=(gate_material.id,),
        )

    # 7. 补正闸门：最晚补正通知尚未被后续事件消除时生效。
    correction = by_type.get("correction")
    if correction is not None:
        later_types = {
            e.event_type
            for e in events
            if e.occurred_at > correction.occurred_at
            or (e.occurred_at == correction.occurred_at and e.seq > correction.seq)
        }
        correction_open = not (later_types & {"accepted", "approved", "licensed", "rejected"})
        if correction_open:
            deadline = correction.detail.get("deadline_at")
            if deadline is not None and at >= deadline:
                return decide(
                    "closed",
                    "correction_overdue",
                    f"已逾补正期限（{deadline.isoformat()}），缓冲终止",
                    events=(correction.id,),
                )
            if not rule.buffer_continues_in_correction:
                return decide(
                    "restricted",
                    "correction_in_progress",
                    "补正期间按规则不得缓冲营业",
                    events=(correction.id,),
                )

    # 8. 监管允许的缓冲窗口（按渠道各自天数，自旧证截止日起算；到期日当天关闭）。
    buffer_days = _channel_buffer_days(rule, channel)
    if accepted is not None:
        buffer_end = license_fact.current_expiry + timedelta(days=buffer_days)
        if at >= buffer_end:
            return decide(
                "closed",
                "buffer_expired",
                f"超过 {buffer_days} 天监管缓冲期（{buffer_end.isoformat()} 到期）",
                events=(accepted.id,),
            )

    if gate_reason == "material_verification_pending":
        return decide(
            "restricted",
            gate_reason,
            f"材料 {gate_material.document_key} 尚在核查",
            materials=(gate_material.id,),
        )

    # 9. 双签放行：合规与业务负责人基于同一案卷版本、非发起人、覆盖该渠道。
    roles, used_signoffs = _valid_signoffs(case, channel)
    used_events = event_ids
    if {"compliance", "business"}.issubset(roles):
        last_sign = max(s.signed_at for s in used_signoffs)
        return decide(
            "open",
            "buffer_released",
            "缓冲窗口内且已取得合规与业务负责人双签放行",
            valid_from=max(last_sign, license_fact.current_expiry),
            events=used_events,
        )

    missing = [r for r in SIGNOFF_ROLES if r not in roles]
    return decide(
        "restricted",
        "awaiting_dual_signoff",
        "已进入监管缓冲期但缺少签署：" + ",".join(missing),
        events=used_events,
    )


def evaluate(
    license_fact: LicenseFact,
    rule: RuleFact | None,
    case: CaseContext | None,
    at,
) -> tuple[ChannelDecision, ...]:
    return tuple(
        evaluate_channel(channel, license_fact, rule, case, at) for channel in CHANNELS
    )
