from juya_admin_api.modules.campaigns.domain import CampaignDuration, CampaignVersion
from juya_admin_api.shared.errors import AppError


class CampaignService:
    @staticmethod
    def revise_version(
        current: CampaignVersion,
        *,
        duration_days: CampaignDuration,
        activation_window_days: int,
        capacity: int,
        scene_ids: tuple[str, ...],
    ) -> CampaignVersion:
        if current.locked_at is not None and (
            duration_days != current.duration_days
            or activation_window_days != current.activation_window_days
            or scene_ids != current.scene_ids
        ):
            raise AppError(
                "CAMPAIGN_VERSION_LOCKED",
                "活动首次开通后时长、启动窗口和场景不可修改",
                409,
            )
        current.duration_days = duration_days
        current.activation_window_days = activation_window_days
        current.capacity = capacity
        current.scene_ids = scene_ids
        current.validate()
        return current
