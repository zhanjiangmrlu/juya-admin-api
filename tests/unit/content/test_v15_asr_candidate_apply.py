"""候选回写必须共享租约事务且重验依赖,付费期间无关编辑不得作废。"""

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from juya_admin_api.integrations.asr.aliyun import ASRCandidate, ASRSegment
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_candidates import CandidateService, dependency
from juya_admin_api.modules.content.v15_candidates_router import create_candidate_router
from juya_admin_api.modules.content_import.stages import Claim
from juya_admin_api.shared.errors import AppError, install_error_handlers


class Result:
    def __init__(self, row=None):
        # 功能:提供受控的当前数据库行。
        self.row, self.rowcount = row, 1

    def mappings(self):
        # 功能:提供生产仓库映射行接口。
        return self

    def first(self):
        # 功能:返回当前受控行。
        return self.row


class Session:
    def __init__(self):
        # 功能:保存真实生产守卫将消费的草稿和已写候选结果。
        self.content = SceneContent.model_validate(
            {
                "dialogue": [{"id": "first", "english": "Hello", "chinese": "你好"}],
                "audio": {
                    "target_id": "target",
                    "version_id": "audio-v1",
                    "asset_id": "asset",
                    "duration_ms": 1000,
                },
            }
        )
        self.version = 9
        self.sha = hashlib.sha256(b"audio").hexdigest()
        self.valid_fence = True
        self.saved = None
        self.applied = False
        self.in_transaction = False
        self.latest = None
        self.latest_hash = None
        self.saved_status = "SUBMITTED"
        self.queries = []

    def begin(self):
        # 功能:提供共享事务。
        return self

    async def __aenter__(self):
        # 功能:标记进入事务上下文。
        self.in_transaction = True
        return self

    async def __aexit__(self, *args):
        # 功能:标记退出事务上下文。
        self.in_transaction = False

    async def scalar(self, statement, params=None):
        # 功能:真实生产fence/audio守卫在受控SQL边界获取资料。
        query = str(statement)
        self.queries.append(query)
        if "FROM content_import_stage" in query:
            return 1 if self.valid_fence else None
        if "FROM audio_version" in query:
            return 2
        raise AssertionError(query)

    async def execute(self, statement, params=None):
        # 功能:保留实际_draft和候选持久化控制流,仅读写外部SQL边界。
        query = str(statement)
        self.queries.append(query)
        if query.startswith("SELECT * FROM lexicon_candidate"):
            return Result(
                {
                    "public_id": "candidate",
                    "revision_id": "revision",
                    "status": self.saved_status,
                    "input_hash": self.latest_hash,
                    "candidate": json.dumps(self.saved),
                }
            )
        if query.startswith("SELECT public_id,stage,status,config_version"):
            return Result(
                {
                    "public_id": "poll",
                    "stage": "ASR_POLL",
                    "status": "SUCCEEDED",
                    "config_version": "v1",
                    "error_code": None,
                }
            )
        if query.startswith("SELECT r.public_id"):
            return Result(
                {
                    "public_id": "revision",
                    "scene_id": "scene",
                    "status": "DRAFT",
                    "edit_version": self.version,
                    "content_snapshot": self.content.model_dump(mode="json"),
                }
            )
        if query.startswith("SELECT revision_id,status,candidate"):
            return Result({"revision_id": "revision", "status": "SUBMITTED", "candidate": {}})
        if query.startswith("SELECT public_id FROM lexicon_candidate"):
            if "hash" in (params or {}) and self.latest_hash != params["hash"]:
                return Result()
            return Result({"public_id": self.latest} if self.latest else None)
        if query.startswith("INSERT INTO lexicon_candidate"):
            self.saved = json.loads(params["payload"])
            return Result()
        if query.startswith("UPDATE lexicon_candidate"):
            self.saved = json.loads(params["payload"])
            self.saved_status = params["status"]
        elif query.startswith("UPDATE content_import_stage"):
            pass
        else:
            raise AssertionError(query)
        return Result()


class Media:
    def __init__(self, session):
        # 功能:替换外部存储读操作,固定可信字节及解码时长。
        self.session = session

    async def get_asset(self, identity):
        # 功能:返回固定源音频的实际字节hash和时长元数据。
        return SimpleNamespace(
            id=identity,
            object_key="audio.wav",
            sha256=self.session.sha,
            duration_ms=1000,
            security_status="PASSED",
        )

    async def read_asset_bytes(self, asset):
        # 功能:提供无需外网的真实音频字节验证边界。
        return b"audio"


def service_context():
    # 功能:构造实际CandidateService与共享受控SQL事务,回调在同一事务修改草稿。
    session = Session()

    async def apply(transaction, revision_id, expected_version, alignment, actor):
        # 功能:模拟authoring外部端口的真实保存效果,验证入参与事务绑定。
        assert transaction is session and session.in_transaction
        assert revision_id == "revision" and expected_version == 9 and actor == "admin"
        assert alignment == [
            {"sentence_id": "first", "start_ms": 10, "end_ms": 900, "match_score": 1.0}
        ]
        sentence = session.content.dialogue[0]
        sentence.start_ms, sentence.end_ms = 10, 900
        sentence.timing_confirmed, sentence.boundary_verified_by = True, "system:asr-alignment"
        session.version += 1
        session.applied = True
        return session.version

    service = CandidateService(
        lambda: session, Mock(), Mock(), media=Media(session), apply_alignment=apply
    )
    claim = Claim(
        "stage", 1, dependency(session.content, 1, "ASR_BOUNDARY", session.sha), "v1", "worker"
    )
    payload = {
        "kind": "ASR_BOUNDARY",
        "revision_id": "revision",
        "edit_version": 1,
        "actor": "admin",
        "results": {
            "alignment_status": "ALIGNED",
            "alignment": [
                {"sentence_id": "first", "start_ms": 10, "end_ms": 900, "match_score": 1.0}
            ],
            "issues": [],
        },
    }
    return session, service, claim, payload


@pytest.mark.asyncio
async def test_ready_applies_to_current_edit_version_within_same_fenced_transaction():
    # 功能:正文依赖不变但编辑号增加时正常应用,同事务记录新编辑号并保留未人工试听事实。
    session, service, claim, payload = service_context()
    await service._save("candidate", payload, "READY", claim)
    assert session.applied and session.version == 10
    assert session.saved["results"]["applied_edit_version"] == 10
    assert not session.saved["results"]["human_listening_confirmed"]
    assert session.saved["applied_input_hash"] == dependency(
        session.content, 10, "ASR_BOUNDARY", session.sha
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["english", "audio", "timing", "fence"])
async def test_ready_stale_result_cannot_apply_or_replace_candidate(change):
    # 功能:云调用期间正文/音频/手动边界变化或租约失效,最终回写拒绝且无保存。
    session, service, claim, payload = service_context()
    if change == "english":
        session.content.dialogue[0].english = "Different"
    elif change == "audio":
        session.content.audio.version_id = "audio-v2"
    elif change == "timing":
        session.content.dialogue[0].start_ms, session.content.dialogue[0].end_ms = 20, 500
    else:
        session.valid_fence = False
    with pytest.raises(AppError) as stale:
        await service._save("candidate", payload, "READY", claim)
    assert stale.value.code == (
        "IMPORT_STAGE_LEASE_LOST" if change == "fence" else "CANDIDATE_INPUT_CHANGED"
    )
    assert not session.applied and session.saved is None
    if change != "fence":
        assert payload["results"]["alignment_status"] == "NEEDS_REVIEW"
        assert payload["results"]["issues"][0]["code"] == "ASR_INPUT_CHANGED"


@pytest.mark.asyncio
async def test_latest_candidate_missing_is_actionable_404():
    # 功能:当前草稿无ASR候选时返回明确404,不伪造旧候选或成功。
    _session, service, _, _ = service_context()
    with pytest.raises(AppError) as missing:
        await service.get_latest("revision", "ASR_BOUNDARY")
    assert missing.value.code == "CANDIDATE_NOT_FOUND" and missing.value.status_code == 404


@pytest.mark.asyncio
async def test_unconfigured_applicator_returns_diagnostic_instead_of_claiming_applied():
    # 功能:没有自动应用端口时保留候选诊断,不假装已写回或完成试听。
    session, service, claim, payload = service_context()
    service.apply_alignment = None
    await service._save("candidate", payload, "READY", claim)
    result = session.saved["results"]
    assert result["alignment_status"] == "NEEDS_REVIEW" and result["alignment"] == []
    assert result["issues"][0]["code"] == "ASR_ALIGNMENT_APPLIER_UNAVAILABLE"
    assert result["applied_edit_version"] is None and not session.applied


@pytest.mark.asyncio
async def test_applied_candidate_reused_after_unrelated_save_and_slice_edit_versions():
    # 功能:自动边界应用后,切片/词条保存增加编辑号不生成新的付费阶段。
    session, service, claim, payload = service_context()
    await service._save("candidate", payload, "READY", claim)
    session.latest, session.latest_hash = "candidate", session.saved["applied_input_hash"]
    session.version = 15
    session.content.dialogue[0].slice_version = "generated-file"
    session.content.title_en = "Updated title"
    service.asr, service.asr_daily_seconds_limit = Mock(), 100
    service.stages = AsyncMock()
    service.stages.ensure_within.return_value = "new-stage"
    reused = await service.queue("revision", "ASR_BOUNDARY", 15, "admin", datetime.now(UTC))
    assert reused == "candidate"
    session.content.dialogue[0].start_ms = 20
    changed = await service.queue("revision", "ASR_BOUNDARY", 15, "admin", datetime.now(UTC))
    assert changed == "new-stage"
    assert session.saved["source_sentences"] == [{"sentence_id": "first", "english": "Hello"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["english", "audio", "timing", "metadata", "slice"])
async def test_latest_asr_view_reports_current_dependency_without_writes_or_false_staleness(change):
    # 功能:只读刷新识别正文/源音频/人工边界陈旧,无关元数据与切片挂回仍保留有效结果。
    session, service, claim, payload = service_context()
    await service._save("candidate", payload, "READY", claim)
    session.latest, session.latest_hash = "candidate", claim.input_hash
    if change == "english":
        session.content.dialogue[0].english = "Different"
    elif change == "audio":
        session.content.audio.version_id = "audio-v2"
    elif change == "timing":
        session.content.dialogue[0].start_ms = 20
    elif change == "metadata":
        session.content.title_en = "Updated title"
        session.content.dialogue[0].chinese = "您好"
        session.version += 3
    else:
        session.content.dialogue[0].slice_version = "generated-slice"
        session.version += 3
    saved = deepcopy(session.saved)
    session.queries.clear()
    view = await service.get_latest("revision", "ASR_BOUNDARY")
    result = view["candidate"]["results"]
    assert view["stale"] is (change in {"english", "audio", "timing"})
    if change in {"english", "audio", "timing"}:
        assert result["alignment_status"] == "NEEDS_REVIEW"
        assert result["issues"][0]["code"] == "ASR_INPUT_CHANGED"
        assert result["alignment"] == []
        assert result["proposed_alignment"] == saved["results"]["alignment"]
    else:
        assert result["alignment_status"] == "ALIGNED" and result["issues"] == []
        assert result["applied_edit_version"] == 10
    assert session.saved == saved
    assert all(
        q.startswith("SELECT") and "FOR UPDATE" not in q and "FOR SHARE" not in q
        for q in session.queries
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["SUBMITTED", "FAILED"])
@pytest.mark.parametrize("change", ["metadata", "timing"])
async def test_pending_and_failed_asr_staleness_uses_inputs_not_origin_edit_version(status, change):
    # 功能:等待/失败候选可跨无关编辑恢复,但人工边界变化必须通过显式stale阻止旧任务恢复。
    session, service, claim, _payload = service_context()
    session.saved = {"kind": "ASR_BOUNDARY", "edit_version": 1, "poll_stage_id": "poll"}
    session.saved_status, session.latest, session.latest_hash = (
        status,
        "candidate",
        claim.input_hash,
    )
    session.version = 15
    if change == "metadata":
        session.content.title_en = "Changed title"
        session.content.dialogue[0].chinese = "您好"
    else:
        session.content.dialogue[0].start_ms, session.content.dialogue[0].end_ms = 20, 800
    original = deepcopy(session.saved)
    view = await service.get_latest("revision", "ASR_BOUNDARY")
    assert view["stale"] is (change == "timing")
    assert view["status"] == status and view["candidate"]["edit_version"] == 1
    assert session.saved == original


@pytest.mark.asyncio
async def test_actual_poll_run_stale_input_persists_review_diagnostic_without_draft_write():
    # 功能:真实run及_save在云结果返回期间发现旧正文,记录需复核诊断且不写当前草稿。
    session, service, claim, _payload = service_context()
    session.saved = {
        "kind": "ASR_BOUNDARY",
        "revision_id": "revision",
        "poll_stage_id": "stage",
        "provider_task_id": "known-task",
        "usage_attempt_id": "attempt",
        "actor": "admin",
    }
    session.latest_hash = claim.input_hash
    stages = AsyncMock()
    stages.claim.return_value = claim
    service.stages = stages
    billed = []

    class Usage:
        async def record_within(self, *args, **kwargs):
            # 功能:保留真实服务给出的付费结算事实,替换外部SQL计费持久化边界。
            billed.append(kwargs)

    class ASR:
        async def poll(self, task_id):
            # 功能:模拟云返回之前当前英文已保存新内容,不替换run的最终写守卫。
            assert task_id == "known-task"
            session.content.dialogue[0].english = "Different"
            return {
                "output": {"task_status": "SUCCEEDED", "results": [{"transcription_url": "result"}]}
            }

        async def parse_result(self, url):
            # 功能:提供原输入的真实时间证据,不会对应已修改正文。
            return ASRCandidate("Hello", (), segments=(ASRSegment("Hello", 10, 900, channel_id=0),))

    service.asr, service.usage = ASR(), Usage()
    with pytest.raises(AppError) as stale:
        await service.run("stage", "worker", datetime.now(UTC))
    assert stale.value.code == "CANDIDATE_INPUT_CHANGED"
    assert session.saved_status == "FAILED" and not session.applied
    assert session.content.dialogue[0].english == "Different" and session.version == 9
    result = session.saved["results"]
    assert result["alignment_status"] == "NEEDS_REVIEW" and result["alignment"] == []
    assert result["issues"][0]["code"] == "ASR_INPUT_CHANGED"
    assert result["applied_edit_version"] is None
    assert billed[-1]["status"] == "SUCCEEDED"


def test_latest_candidate_route_uses_read_permission_and_validates_kind():
    # 功能:页面刷新通过只读接口恢复当前revision/kind候选,未知kind拒绝。
    class Service:
        async def get_latest(self, revision_id, kind):
            # 功能:提供受控现有候选状态,使实际路由/验证对外可测。
            return {"id": "candidate", "revision_id": revision_id, "kind": kind}

    async def admin():
        # 功能:提供路由测试的已鉴权管理员。
        return SimpleNamespace(admin_user_id=7)

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(
        create_candidate_router(Service(), current_admin=admin, current_admin_write=admin)
    )
    with TestClient(app) as client:
        path = "/api/v1/admin/content/revisions/revision/candidates/latest"
        response = client.get(path, params={"kind": "ASR_BOUNDARY"})
        assert response.status_code == 200 and response.json()["revision_id"] == "revision"
        assert client.get(path, params={"kind": "INVALID"}).status_code == 422
