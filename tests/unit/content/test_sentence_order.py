"""Dialogue timeline validation must reject reversed or overlapping sentence intervals."""

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent


def test_reversed_and_overlapping_timeline_cannot_pass_timing_check():
    # 相同源音频和已确认事实仍不能使倒序或重叠的两句通过发布时间校验
    asset = {
        "status": "CONFIRMED",
        "security_status": "PASSED",
        "asset_type": "audio",
        "object_key": "sealed/media/audio/whole.wav",
        "duration_ms": 4000,
    }
    version = {"status": "CONFIRMED", "target_id": "t", "asset_id": "a", "target_type": "WHOLE"}
    for intervals in [[(2000, 3000), (0, 1000)], [(0, 2000), (1000, 3000)]]:
        content = SceneContent.model_validate(
            {
                "schema_version": 2,
                "audio": {
                    "target_id": "t",
                    "version_id": "v",
                    "asset_id": "a",
                    "duration_ms": 4000,
                },
                "dialogue": [
                    {
                        "id": str(index),
                        "speaker": "A",
                        "english": "Hello",
                        "chinese": "你好",
                        "start_ms": start,
                        "end_ms": end,
                        "audio_version_id": "v",
                        "timing_confirmed": True,
                    }
                    for index, (start, end) in enumerate(intervals)
                ],
            }
        )
        checks = {
            check.code: check.passed
            for check in check_content(content, {"a": asset}, {"v": version})
        }
        assert checks["SENTENCE_TIMING_INVALID"] is False
