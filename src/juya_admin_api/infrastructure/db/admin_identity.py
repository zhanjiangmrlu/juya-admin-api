from collections.abc import Collection

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession


async def resolve_admin_names(
    session: AsyncSession, actors: Collection[str | None]
) -> dict[str, str]:
    # 功能: 批量解析管理员内部主键及公开编号对应的真实账号名,不改写原始身份
    # 参数:
    #     session: 当前只读数据库会话
    #     actors: 待解析的审计主体标识,系统及空标识不参与账号查询
    # 返回: 可解析的主体标识到账号名的映射,未知身份不生成名称
    identifiers = tuple({actor for actor in actors if actor and actor != "system"})
    if not identifiers:
        return {}
    rows = (
        (
            await session.execute(
                text(
                    "SELECT public_id,CAST(id AS CHAR) AS internal_id,username FROM admin_user "
                    "WHERE public_id IN :actors OR CAST(id AS CHAR) IN :actors"
                ).bindparams(bindparam("actors", expanding=True)),
                {"actors": identifiers},
            )
        )
        .mappings()
        .all()
    )
    names = {row["internal_id"]: row["username"] for row in rows}
    names.update({row["public_id"]: row["username"] for row in rows})
    return names
