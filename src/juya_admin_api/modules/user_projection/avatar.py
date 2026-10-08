from collections.abc import Awaitable, Callable


async def profile_avatar_url(
    user_id: str, object_key: str, sign: Callable[[str, int], Awaitable[str]]
) -> str | None:
    # 功能: 仅为用户本人规范头像路径签发短期地址,拒绝任意私有素材.
    # 参数:
    #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
    #     object_key: OSS 桶内对象键,可包含固定素材版本定位信息.
    #     sign: 按 OSS 对象键和有效秒数签发下载地址的异步回调.
    # 返回: 规范头像路径的短期签名地址;路径越界时为 None.
    """Sign only the canonical user's avatar namespace, never arbitrary private assets."""
    if not object_key.startswith(f"avatars/{user_id}/") or any(
        part in {"", ".", ".."} for part in object_key.split("/")
    ):
        return None
    return await sign(object_key, 300)
