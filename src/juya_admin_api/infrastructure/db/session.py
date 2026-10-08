from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    # 功能: 按连接地址创建带连接健康检查的异步数据库引擎.
    # 参数:
    #     database_url: SQLAlchemy 数据库连接地址.
    #     echo: 是否输出 SQLAlchemy 执行的 SQL 日志.
    # 返回: 创建的异步数据库引擎.
    return create_async_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # 功能: 创建不在提交时过期对象的异步数据库会话工厂.
    # 参数:
    #     engine: SQLAlchemy 异步数据库引擎.
    # 返回: 用于创建异步数据库会话的工厂.
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    # 功能: 管理异步数据库会话和事务的生命周期.
    # 参数:
    #     factory: 创建 SQLAlchemy 异步数据库会话的工厂.
    # 返回: 上下文中可使用的异步数据库会话,退出时结束事务并关闭.
    async with factory() as session:
        yield session
