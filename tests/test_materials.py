"""材料去重、内容变化核查、案卷版本递增。"""

from tests.conftest import basic_material, seed_world


def test_identical_resubmission_reuses_record(svc):
    seed_world(svc)
    svc.submit_batch(
        docket_no="D1", store_code="S1", license_number="L1", operator_code="OP1",
        created_by="alice", materials_doc=[basic_material("app_form", "h1")],
    )
    result = svc.add_materials("D1", [basic_material("app_form", "h1")])
    assert result == {"docket_no": "D1", "changed": 0, "resubmitted": 1}

    status = svc.channel_status("S1")
    mats = status["persisted_rulings"]["physical"]["materials"]
    app_form = [m for m in mats if m["doc_key"] == "app_form"][0]
    assert app_form["state"] == "accepted"


def test_content_change_enters_review_and_bumps_revision(svc):
    seed_world(svc)
    svc.submit_batch(
        docket_no="D1", store_code="S1", license_number="L1", operator_code="OP1",
        created_by="alice", materials_doc=[basic_material("app_form", "h1")],
    )
    rev_before = svc.channel_status("S1")["dossier_revision"]

    changed = basic_material("app_form", "h2-different")
    result = svc.add_materials("D1", [changed])
    assert result["changed"] == 1
    assert svc.channel_status("S1")["dossier_revision"] == rev_before + 1

    state = svc.channel_status("S1")["channels"]
    # 材料核查中：原本受理后可开放的渠道收紧为受限。
    assert all(state[c]["stage"] == "material_review" for c in state)
    assert all(state[c]["state"] == "restricted" for c in state)


def test_review_resolution_restores_and_bumps(svc):
    seed_world(svc)
    svc.submit_batch(
        docket_no="D1", store_code="S1", license_number="L1", operator_code="OP1",
        created_by="alice", materials_doc=[basic_material("app_form", "h1")],
    )
    svc.add_materials("D1", [basic_material("app_form", "h2")])
    out = svc.resolve_material_review("D1", "app_form", "accepted")
    assert out["state"] == "accepted"
    stage = svc.channel_status("S1")["channels"]["physical"]["stage"]
    assert stage == "submitted"


def test_batch_idempotent_on_docket_no(svc):
    seed_world(svc)
    payload = dict(
        docket_no="D1", store_code="S1", license_number="L1", operator_code="OP1",
        created_by="alice", materials_doc=[basic_material()],
    )
    first = svc.submit_batch(**payload)
    second = svc.submit_batch(**payload)
    assert first["id"] == second["id"]
    assert second["deduplicated"] is True
