def security_status_usable(status: object, *, require_review: bool = True) -> bool:
    # 功能: 按审核开关判定媒体安全状态是否允许使用.
    # 参数:
    #     status: 素材审核状态,例如 PASSED,SKIPPED 或 BLOCKED.
    #     require_review: 是否要求实际内容安全审核通过;关闭时允许 SKIPPED.
    # 返回: 审核通过或关闭审核且状态为 SKIPPED 时为 True.
    """Skipped moderation is usable only while the deployment has review disabled."""
    return status == "PASSED" or (not require_review and status == "SKIPPED")
