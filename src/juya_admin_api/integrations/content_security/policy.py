def security_status_usable(status: object, *, require_review: bool = True) -> bool:
    """Skipped moderation is usable only while the deployment has review disabled."""
    return status == "PASSED" or (not require_review and status == "SKIPPED")
