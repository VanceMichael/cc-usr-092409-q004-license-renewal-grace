"""地区规则与渠道临时状态裁定引擎（纯函数，无 I/O）。

规则以 JSON 存于 ``rule_versions.rules``，案卷提交时锁定版本。引擎只根据
调用方组装好的“截至某时点已知事实快照”计算三渠道状态，因此：

* 重放任意 as-of 时点只需过滤 ``recorded_at <= as_of`` 的事实；
* 迟到事件在当时不可见，自然无法倒改已作出的裁定；
* 缓冲到期/补正逾期由绝对截止时间与时钟比较得出，不依赖进程内定时器。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .clock import add_days

CHANNELS = ("physical", "online", "delivery")
OPEN, RESTRICTED, CLOSED = "open", "restricted", "closed"

# 阶段（案卷生命周期 + 时钟派生阶段）。
VALID_LICENSE = "valid_license"
SUBMITTED = "submitted"
ACCEPTED = "accepted"
GRACE_EXPIRED = "grace_expired"
CORRECTION = "correction"
CORRECTION_OVERDUE = "correction_overdue"
MATERIAL_REVIEW = "material_review"
APPROVED = "approved"
ISSUED = "issued"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
NO_RENEWAL = "no_renewal_after_expiry"
SUSPENDED = "license_suspended"
TRANSFERRED = "license_transferred"

DEFAULT_RULES: dict = {
    "grace_days": 30,
    "correction_days": 15,
    "dual_signoff_required": True,
    # 实体店最宽、线上店次之、配送最严：同一案卷阶段下资格可不同。
    "channels": {
        "physical": {
            VALID_LICENSE: OPEN,
            SUBMITTED: RESTRICTED,
            ACCEPTED: OPEN,
            GRACE_EXPIRED: CLOSED,
            CORRECTION: RESTRICTED,
            CORRECTION_OVERDUE: CLOSED,
            MATERIAL_REVIEW: RESTRICTED,
            APPROVED: OPEN,
            ISSUED: OPEN,
            REJECTED: CLOSED,
            WITHDRAWN: CLOSED,
            NO_RENEWAL: CLOSED,
        },
        "online": {
            VALID_LICENSE: OPEN,
            SUBMITTED: CLOSED,
            ACCEPTED: RESTRICTED,
            GRACE_EXPIRED: CLOSED,
            CORRECTION: RESTRICTED,
            CORRECTION_OVERDUE: CLOSED,
            MATERIAL_REVIEW: RESTRICTED,
            APPROVED: OPEN,
            ISSUED: OPEN,
            REJECTED: CLOSED,
            WITHDRAWN: CLOSED,
            NO_RENEWAL: CLOSED,
        },
        "delivery": {
            VALID_LICENSE: OPEN,
            SUBMITTED: CLOSED,
            ACCEPTED: RESTRICTED,
            GRACE_EXPIRED: CLOSED,
            CORRECTION: CLOSED,
            CORRECTION_OVERDUE: CLOSED,
            MATERIAL_REVIEW: RESTRICTED,
            APPROVED: RESTRICTED,
            ISSUED: OPEN,
            REJECTED: CLOSED,
            WITHDRAWN: CLOSED,
            NO_RENEWAL: CLOSED,
        },
    },
}

# 监管动作对全渠道的覆盖状态。
_ACTION_STATE = {
    "suspended": (SUSPENDED, CLOSED),
    "transferred": (TRANSFERRED, CLOSED),
}


def determine_stage(snapshot: Mapping, as_of: str) -> tuple[str, dict]:
    """重放截至 as_of 系统已知悉的事件，返回 (阶段, 派生时间线)。

    事件在 ``recorded_at``（系统收到纸质回执/通知的时点）生效，因此只使用
    ``recorded_at <= as_of`` 的事件——迟到回执在历史时点尚不可见，绝不倒改
    当时的决定。事件严格按知悉顺序（recorded_at, seq）重放；``occurred_at``
    只是文书载明时点，用于起算缓冲期与补正期限等绝对截止时间。
    """
    events = [e for e in snapshot["events"] if e["recorded_at"] <= as_of]
    events.sort(key=lambda e: (e["recorded_at"], e["seq"]))

    stage = VALID_LICENSE if as_of < snapshot["license"]["expires_at"] else NO_RENEWAL
    timeline: dict = {}

    for event in events:
        kind = event["kind"]
        at = event["occurred_at"]
        if kind == "submitted":
            if stage in (VALID_LICENSE, NO_RENEWAL, SUBMITTED):
                stage = SUBMITTED
        elif kind == "accepted":
            stage = ACCEPTED
            timeline["grace_end"] = add_days(at, snapshot["rules"]["grace_days"])
            timeline["accepted_event"] = event
        elif kind == "correction_request":
            stage = CORRECTION
            timeline["correction_due"] = add_days(at, snapshot["rules"]["correction_days"])
            timeline["correction_event"] = event
        elif kind == "answered":
            # 补正回复后回到受理态，原补正期限作废。
            stage = ACCEPTED
            timeline.pop("correction_due", None)
            timeline["answered_event"] = event
        elif kind == "rejected":
            stage = REJECTED
        elif kind == "approved":
            stage = APPROVED
        elif kind == "license_issued":
            stage = ISSUED
        elif kind == "withdrawn":
            stage = WITHDRAWN

    # 时钟派生：缓冲到期与补正逾期。期限为绝对时间，宕机不暂停。
    if stage == ACCEPTED and "grace_end" in timeline and as_of > timeline["grace_end"]:
        stage = GRACE_EXPIRED
    if stage == CORRECTION and "correction_due" in timeline and as_of > timeline["correction_due"]:
        stage = CORRECTION_OVERDUE

    # 已受理/已提交材料内容变化进入核查期间，临时资格收紧。
    provisional = (SUBMITTED, ACCEPTED)
    if stage in provisional and any(
        m["state"] == "under_review"
        for m in snapshot["materials"]
        if m["first_seen_at"] <= as_of
    ):
        stage = MATERIAL_REVIEW

    return stage, timeline


def current_license_action(snapshot: Mapping, as_of: str) -> str | None:
    """截至 as_of 最新知悉的许可证监管动作（按系统知悉顺序）。"""
    actions = [a for a in snapshot["actions"] if a["recorded_at"] <= as_of]
    if not actions:
        return None
    actions.sort(key=lambda a: (a["recorded_at"], a["id"]))
    return actions[-1]["kind"]


def signoff_status(snapshot: Mapping) -> tuple[bool, str]:
    """缓冲放行双签校验：同一案卷版本、合规与业务分别签署、发起人不得自批。

    返回 (是否满足, 原因代码)。两个签署人必须不同，且都不能是发起人。
    """
    dossier = snapshot["dossier"]
    if not snapshot["rules"].get("dual_signoff_required", True):
        return True, "signoff_not_required"

    revision = dossier["revision"]
    current = [s for s in snapshot["signoffs"] if s["revision"] == revision]
    by_role = {s["role"]: s for s in current}
    initiator = dossier["created_by"]

    missing = [role for role in ("compliance", "business") if role not in by_role]
    if missing:
        return False, "awaiting_signoff:" + ",".join(missing)

    compliance_signer = by_role["compliance"]["signer"]
    business_signer = by_role["business"]["signer"]
    if compliance_signer == initiator or business_signer == initiator:
        return False, "initiator_self_approval_blocked"
    if compliance_signer == business_signer:
        return False, "signers_must_differ"
    return True, "dual_signoff_complete"


def evaluate(snapshot: Mapping, as_of: str) -> dict[str, dict]:
    """计算三渠道在 as_of 时点的临时状态与完整依据。

    返回 ``{channel: {state, reason, stage, rule_state, signoff, material_ids,
    event_ids, deadlines}}``。
    """
    rules = snapshot["rules"]
    stage, timeline = determine_stage(snapshot, as_of)
    action = current_license_action(snapshot, as_of)
    signoff_ok, signoff_reason = signoff_status(snapshot)

    known_events = [e["id"] for e in snapshot["events"] if e["recorded_at"] <= as_of]
    material_ids = [
        m["id"] for m in snapshot["materials"] if m["first_seen_at"] <= as_of
    ]

    result = {}
    for channel in CHANNELS:
        channel_rules = rules["channels"][channel]

        if action in _ACTION_STATE:
            override_stage, state = _ACTION_STATE[action]
            reason = override_stage
        else:
            override_stage = None
            state = channel_rules[stage]
            reason = stage

        # 新证到达（issued）与旧证本身有效（valid_license）无需缓冲签署；
        # 其余任何“凭临时依据开放”都必须双签齐备，否则降级为受限。
        temporary_open = state == OPEN and stage not in (ISSUED, VALID_LICENSE)
        if not override_stage and temporary_open and not signoff_ok:
            state = RESTRICTED
            reason = signoff_reason

        result[channel] = {
            "channel": channel,
            "state": state,
            "reason": reason,
            "stage": override_stage or stage,
            "rule_state": channel_rules[stage],
            "signoff": signoff_reason if state != OPEN or temporary_open else "not_needed",
            "material_ids": material_ids,
            "event_ids": known_events,
            "deadlines": {k: v for k, v in timeline.items() if not str(k).endswith("event")},
        }
    return result


def assemble_snapshot(
    *,
    license_row: Mapping,
    dossier_row: Mapping | None,
    rules: Mapping,
    events: Sequence[Mapping],
    materials: Sequence[Mapping],
    signoffs: Sequence[Mapping],
    actions: Sequence[Mapping],
) -> Mapping:
    """组装引擎快照。dossier 为空表示该许可证尚无生效案卷。"""
    if dossier_row is None:
        dossier_row = {
            "id": None,
            "revision": 0,
            "created_by": "",
            "rule_version_id": None,
        }
        signoffs = []
    return {
        "license": dict(license_row),
        "dossier": dict(dossier_row),
        "rules": dict(rules),
        "events": [dict(e) for e in events],
        "materials": [dict(m) for m in materials],
        "signoffs": [dict(s) for s in signoffs],
        "actions": [dict(a) for a in actions],
    }
