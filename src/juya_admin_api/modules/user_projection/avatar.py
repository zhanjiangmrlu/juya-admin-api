from collections.abc import Awaitable, Callable


async def profile_avatar_url(
    user_id: str, object_key: str, sign: Callable[[str, int], Awaitable[str]]
) -> str | None:
    """Sign only the canonical user's avatar namespace, never arbitrary private assets."""
    if not object_key.startswith(f"avatars/{user_id}/") or any(
        part in {"", ".", ".."} for part in object_key.split("/")
    ):
        return None
    return await sign(object_key, 300)
