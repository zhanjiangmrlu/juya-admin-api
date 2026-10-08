from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from juya_admin_api.shared.errors import AppError

CampaignDuration = Literal[3, 5]


@dataclass(slots=True)
class CampaignVersion:
    id: str
    campaign_id: str
    status: str
    duration_days: CampaignDuration
    activation_window_days: int
    capacity: int
    granted_user_count: int
    scene_ids: tuple[str, ...]
    version: int = 1
    locked_at: datetime | None = None

    def validate(self) -> None:
        # 功能: 校验活动版本天数,启动窗口及容量约束.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        if self.duration_days not in {3, 5}:
            raise AppError("CAMPAIGN_DURATION_INVALID", "限时活动只允许 3 天或 5 天", 422)
        if self.activation_window_days < 1:
            raise AppError("CAMPAIGN_START_WINDOW_INVALID", "启动窗口至少为 1 天", 422)
        if self.capacity < self.granted_user_count:
            raise AppError(
                "CAMPAIGN_CAPACITY_BELOW_GRANTED",
                "容量不能低于累计开通人数",
                409,
            )
