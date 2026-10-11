from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from juya_admin_api.api.internal.dependencies import create_service_auth_dependency
from juya_admin_api.infrastructure.config import Settings
from juya_admin_api.infrastructure.db.schema_version import check_minimum_schema_version
from juya_admin_api.infrastructure.db.session import create_engine, create_session_factory
from juya_admin_api.infrastructure.security.service_hmac import RedisNonceStore, ServicePrincipal
from juya_admin_api.infrastructure.v15_runtime import V15Runtime, build_v15_services, tts_config
from juya_admin_api.integrations.content_security.aliyun import create_content_security_provider
from juya_admin_api.integrations.miniapp_api.client import MiniappApiClient
from juya_admin_api.integrations.oss.aliyun import AliyunOssProvider
from juya_admin_api.integrations.oss.credentials import ControlledCredentialsProvider
from juya_admin_api.modules.access_policy.router import create_internal_content_router
from juya_admin_api.modules.access_policy.service import AccessPolicyService
from juya_admin_api.modules.admin_auth.domain import SessionRecord
from juya_admin_api.modules.admin_auth.repository import TransactionalAdminAuthRepository
from juya_admin_api.modules.admin_auth.router import (
    ADMIN_SESSION_COOKIE,
    create_admin_security_router,
)
from juya_admin_api.modules.admin_auth.service import AdminAuthService
from juya_admin_api.modules.analytics.router import create_analytics_router
from juya_admin_api.modules.analytics.service import SQLAlchemyAnalyticsRepository
from juya_admin_api.modules.audit.service import AuditService, SQLAlchemyAuditRepository
from juya_admin_api.modules.campaigns.repository import SQLAlchemyCampaignRepository
from juya_admin_api.modules.campaigns.router import create_campaign_router
from juya_admin_api.modules.campaigns.service import CampaignService
from juya_admin_api.modules.cloud_usage.router import create_usage_router
from juya_admin_api.modules.contacts.router import create_contact_router
from juya_admin_api.modules.contacts.service import ContactAdminService
from juya_admin_api.modules.content.production_router import create_production_content_router
from juya_admin_api.modules.content.publication_ranges_router import create_publication_range_router
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.router import create_content_router
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.modules.content.v15_authoring_router import create_authoring_router
from juya_admin_api.modules.content.v15_candidates_router import create_candidate_router
from juya_admin_api.modules.content.v15_materials_router import create_materials_router
from juya_admin_api.modules.content.v15_proofreading_router import create_proofreading_router
from juya_admin_api.modules.content_import.admin_stages import StageAdminStore
from juya_admin_api.modules.content_import.operations_router import create_import_operations_router
from juya_admin_api.modules.content_import.preview import ImportPreviewStore
from juya_admin_api.modules.content_import.router import create_import_router
from juya_admin_api.modules.dashboard.service import (
    DashboardService,
    SQLAlchemyDashboardRepository,
)
from juya_admin_api.modules.feedback.repository import SQLAlchemyFeedbackRepository
from juya_admin_api.modules.feedback.router import (
    create_admin_feedback_router,
    create_internal_feedback_router,
)
from juya_admin_api.modules.feedback.service import FeedbackService
from juya_admin_api.modules.formal_entitlements.repository import (
    SQLAlchemyEntitlementQueryRepository,
    SQLAlchemyFormalEntitlementRepository,
    SQLAlchemyFormalGrantPort,
)
from juya_admin_api.modules.formal_entitlements.router import (
    create_entitlement_query_router,
    create_formal_entitlement_router,
)
from juya_admin_api.modules.formal_entitlements.service import FormalEntitlementService
from juya_admin_api.modules.learning_features.modules import LearningModulesStore
from juya_admin_api.modules.learning_features.modules_router import create_module_admin_router
from juya_admin_api.modules.learning_features.router import create_learning_features_router
from juya_admin_api.modules.learning_features.store import LearningFeaturesStore
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
    SQLAlchemyLimitedGrantPort,
)
from juya_admin_api.modules.limited_entitlements.router import (
    create_limited_entitlement_router,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.modules.media.repository import (
    SQLAlchemyMediaAdminRepository,
    SQLAlchemyMediaRepository,
    SQLAlchemySignedTargetResolver,
)
from juya_admin_api.modules.media.router import (
    create_internal_media_router,
    create_media_router,
)
from juya_admin_api.modules.media.service import MediaAdminService, MediaService
from juya_admin_api.modules.media.tasks import CeleryMediaTaskDispatcher
from juya_admin_api.modules.pronunciation.admin_router import create_pronunciation_admin_router
from juya_admin_api.modules.pronunciation.admin_store import PronunciationAdminStore
from juya_admin_api.modules.pronunciation.router import create_router as create_pronunciation_router
from juya_admin_api.modules.short_reading.router import create_short_reading_router
from juya_admin_api.modules.short_reading.upload_targets import create_short_reading_upload_router
from juya_admin_api.modules.system_config.service import (
    SQLAlchemySystemConfigRepository,
    SystemConfigService,
)
from juya_admin_api.modules.user_projection.avatar import profile_avatar_url
from juya_admin_api.modules.user_projection.deletion_service import (
    DeletionCleanupService,
    SQLAlchemyDeletionRepository,
)
from juya_admin_api.modules.user_projection.repository import (
    SQLAlchemyUserProjectionRepository,
)
from juya_admin_api.modules.user_projection.router import (
    create_internal_deletion_router,
    create_operations_router,
)
from juya_admin_api.modules.user_projection.service import UserProjectionService
from juya_admin_api.modules.v15_access.router import create_v15_access_router
from juya_admin_api.modules.work_items.repository import SQLAlchemyWorkItemSource
from juya_admin_api.modules.work_items.service import WorkItemService
from juya_admin_api.shared.errors import AppError


@dataclass(slots=True)
class Runtime:
    routers: tuple[APIRouter, ...]
    readiness_probe: Callable[[], Awaitable[Mapping[str, bool]]]
    engine: AsyncEngine
    redis: Redis
    miniapp_client: MiniappApiClient
    v15: V15Runtime | None = None

    async def close(self) -> None:
        # 功能: 释放应用运行依赖持有的连接和运行资源.
        # 参数:
        #     self: 当前实例,承载本类依赖和运行状态.
        # 返回: 无返回值;正常完成表示本次操作成功.
        await self.miniapp_client.aclose()
        if self.v15:
            await self.v15.client.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def build_runtime(settings: Settings) -> Runtime:
    # 功能: 构建数据库,缓存,云服务,业务服务及路由依赖.
    # 参数:
    #     settings: 已加载并校验的服务运行配置.
    # 返回: 完整的运行时依赖集合.
    settings.validate_oss_configuration()
    database_url = _required_secret(settings.database_url, "JUYA_DATABASE_URL")
    redis_url = _required_secret(settings.redis_url, "JUYA_REDIS_URL")
    internal_secret = settings.internal_hmac_secret
    if internal_secret is None:
        raise ValueError("JUYA_INTERNAL_HMAC_SECRET is required")
    if not settings.oss_region or not settings.oss_bucket:
        raise ValueError("JUYA_OSS_REGION and JUYA_OSS_BUCKET are required")
    engine = create_engine(database_url.replace("mysql+pymysql://", "mysql+asyncmy://", 1))
    sessions = create_session_factory(engine)
    redis = Redis.from_url(redis_url, decode_responses=True)
    nonce_store = RedisNonceStore(redis)
    allowed_services = frozenset(
        item.strip() for item in settings.allowed_internal_services.split(",") if item.strip()
    )
    current_service = create_service_auth_dependency(
        internal_secret,
        nonce_store,
        allowed_services=allowed_services,
    )
    auth = AdminAuthService(TransactionalAdminAuthRepository(sessions))

    async def current_admin(
        session_token: Annotated[str | None, Cookie(alias=ADMIN_SESSION_COOKIE)] = None,
    ) -> SessionRecord:
        # 功能: 根据会话 Cookie 认证管理员身份.
        # 参数:
        #     session_token: 浏览器 Cookie 中的管理员会话令牌明文.
        # 返回: 经过查询或认证的管理员会话记录.
        if session_token is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return await auth.authenticate_session(session_token, datetime.now(UTC))

    async def current_admin_write(
        session: Annotated[SessionRecord, Depends(current_admin)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> SessionRecord:
        # 功能: 验证管理员会话对应的 CSRF 令牌后允许写入.
        # 参数:
        #     session: 管理员会话记录,包含身份,令牌摘要和有效期.
        #     csrf_token: 客户端提交的 CSRF 令牌明文,缺失或与会话不匹配时拒绝写入.
        # 返回: 经过查询或认证的管理员会话记录.
        auth.verify_csrf(session, csrf_token)
        return session

    config = SystemConfigService(SQLAlchemySystemConfigRepository(sessions))
    audit = AuditService(SQLAlchemyAuditRepository(sessions))
    formal = FormalEntitlementService(SQLAlchemyFormalEntitlementRepository(sessions))
    limited = LimitedEntitlementService(SQLAlchemyLimitedEntitlementRepository(sessions))
    entitlement_queries = SQLAlchemyEntitlementQueryRepository(sessions)
    campaigns = CampaignService(SQLAlchemyCampaignRepository(sessions))
    feedback = FeedbackService(
        SQLAlchemyFeedbackRepository(sessions), sla_hours_provider=config.feedback_sla_hours
    )
    oss = AliyunOssProvider(
        settings.oss_region,
        settings.oss_bucket,
        endpoint=settings.oss_endpoint,
        credentials_provider=ControlledCredentialsProvider(
            mode=settings.oss_credentials_mode,
            role_name=settings.oss_ram_role_name,
            access_key_id=settings.oss_access_key_id.get_secret_value()
            if settings.oss_access_key_id
            else None,
            access_key_secret=settings.oss_access_key_secret.get_secret_value()
            if settings.oss_access_key_secret
            else None,
            security_token=settings.oss_session_token.get_secret_value()
            if settings.oss_session_token
            else None,
            expires_at=settings.oss_credentials_expires_at,
            from_environment=True,
        ),
    )
    security = create_content_security_provider(settings, oss)
    media = MediaService(
        oss,
        SQLAlchemyMediaRepository(sessions),
        signed_url_ttl_seconds=settings.signed_url_ttl_seconds,
        security=security,
        ffprobe_path=settings.ffprobe_path,
        require_review=settings.content_security_enabled,
    )
    v15 = build_v15_services(settings, sessions, oss, media, security)
    content_repository = SQLAlchemyContentRepository(
        sessions,
        require_review=settings.content_security_enabled,
        prepare_asset=media.get_asset,
        check_confirmation=v15.proofreading.is_confirmed_within,
    )
    content = ContentService(
        content_repository,
        module_capabilities={
            "scene_learning": True,
            "short_reading": True,
            "long_sentence": False,
            "grammar": False,
        },
    )
    production_store = v15.production
    access = AccessPolicyService(
        content_repository,
        SQLAlchemyFormalGrantPort(sessions),
        SQLAlchemyLimitedGrantPort(sessions),
    )
    media_admin = MediaAdminService(SQLAlchemyMediaAdminRepository(sessions))
    media_dispatcher = CeleryMediaTaskDispatcher()
    miniapp_client = MiniappApiClient(
        settings.miniapp_api_base_url,
        internal_secret.get_secret_value().encode(),
    )
    contacts = ContactAdminService(miniapp_client, audit)

    async def avatar_provider(user_id: str, key: str) -> str | None:
        # 功能: 仅为当前用户头像命名空间中的对象签发访问地址.
        # 参数:
        #     user_id: 用户公开标识,用于查询用户数据及关联业务记录.
        #     key: 用户头像 OSS 对象键,必须位于该用户规范头像目录.
        # 返回: 规范用户头像的访问地址;非法路径或未配置对象存储时为 None.
        return await profile_avatar_url(user_id, key, oss.sign_get_url)

    users = UserProjectionService(
        SQLAlchemyUserProjectionRepository(sessions),
        miniapp_client,
        audit,
        avatar_provider=avatar_provider,
    )
    dashboard = DashboardService(
        SQLAlchemyDashboardRepository(
            sessions, warning_days_provider=config.entitlement_warning_days
        )
    )
    work_items = WorkItemService(SQLAlchemyWorkItemSource(sessions))
    analytics = SQLAlchemyAnalyticsRepository(sessions)
    deletion = DeletionCleanupService(SQLAlchemyDeletionRepository(sessions))
    assert v15.materials is not None
    assert v15.publication is not None

    async def current_bff(
        principal: Annotated[ServicePrincipal, Depends(current_service)],
    ) -> ServicePrincipal:
        if principal.service_name != "juya-miniapp-api":
            from juya_admin_api.shared.errors import AppError

            raise AppError("SERVICE_FORBIDDEN", "仅受信小程序服务可调用", 403)
        return principal

    async def verify_admin_asset(asset_id: str) -> None:
        asset = await media.get_asset(asset_id)
        metadata = await oss.head_object(asset.object_key)
        if (
            metadata.size != asset.size
            or metadata.sha256 != asset.sha256
            or metadata.content_type.split(";", 1)[0].lower() != asset.content_type.lower()
        ):
            raise AppError("MEDIA_ASSET_CHANGED", "固定素材对象与登记不一致", 409)

    async def sign_admin_asset(asset_id: str, ttl: int) -> str:
        await verify_admin_asset(asset_id)
        asset = await media.get_asset(asset_id)
        return await oss.sign_get_url(asset.object_key, min(ttl, 60))

    routers = (
        create_learning_features_router(
            LearningFeaturesStore(sessions),
            current_admin=current_admin,
            current_admin_write=current_admin_write,
            current_service=current_service,
        ),
        create_module_admin_router(
            LearningModulesStore(
                sessions,
                capabilities={
                    "scene_learning": True,
                    "short_reading": True,
                    "long_sentence": False,
                    "grammar": False,
                },
            ),
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_short_reading_upload_router(
            v15.readings,
            media,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_pronunciation_admin_router(
            PronunciationAdminStore(
                sessions,
                tts_config(settings),
                sign_asset=sign_admin_asset,
                verify_asset=verify_admin_asset,
            ),
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_publication_range_router(
            v15.publication,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_usage_router(sessions, current_admin=current_admin),
        create_materials_router(
            v15.materials, current_admin=current_admin, current_admin_write=current_admin_write
        ),
        create_import_operations_router(
            StageAdminStore(v15.imports, v15.authoring, v15.materials, v15.candidates),
            ImportPreviewStore(
                v15.imports, internal_secret.get_secret_value().encode(), settings.ffprobe_path
            ),
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_import_router(
            v15.imports, current_admin=current_admin, current_admin_write=current_admin_write
        ),
        create_proofreading_router(
            v15.proofreading, current_admin=current_admin, current_admin_write=current_admin_write
        ),
        create_authoring_router(
            v15.authoring, current_admin_write=current_admin_write, current_admin=current_admin
        ),
        create_candidate_router(
            v15.candidates, current_admin=current_admin, current_admin_write=current_admin_write
        ),
        create_short_reading_router(
            v15.readings, current_admin=current_admin, current_admin_write=current_admin_write
        ),
        create_v15_access_router(v15.access, current_service=current_service),
        create_pronunciation_router(v15.pronunciation, current_bff),
        create_production_content_router(
            production_store,
            content,
            media,
            media_admin,
            audit_service=audit,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_analytics_router(analytics, current_admin=current_admin),
        create_admin_security_router(
            auth,
            config,
            audit_service=audit,
            session_cookie_secure=not settings.allow_insecure_http,
        ),
        create_content_router(
            content,
            audit_service=audit,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_formal_entitlement_router(
            formal,
            query_repository=entitlement_queries,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_limited_entitlement_router(
            limited,
            query_repository=entitlement_queries,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_entitlement_query_router(
            entitlement_queries, current_admin=current_admin, users=users
        ),
        create_campaign_router(
            campaigns,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_admin_feedback_router(
            feedback,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
            media_service=media,
            audit_service=audit,
        ),
        create_contact_router(
            contacts,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_media_router(
            media,
            admin_service=media_admin,
            content_service=content,
            audit_service=audit,
            task_dispatcher=media_dispatcher,
            current_admin=current_admin,
            current_admin_write=current_admin_write,
        ),
        create_operations_router(
            users,
            dashboard,
            work_items,
            analytics,
            current_admin=current_admin,
        ),
        create_internal_content_router(
            access,
            content_repository,
            current_service=current_service,
            scene_activation=limited,
            production_store=production_store,
            media_service=media,
        ),
        create_internal_feedback_router(feedback, current_service=current_service),
        create_internal_media_router(
            media,
            resolve_target=SQLAlchemySignedTargetResolver(sessions, access),
            current_service=current_service,
        ),
        create_internal_deletion_router(deletion, current_service=current_service),
    )

    async def readiness() -> Mapping[str, bool]:
        # 功能: 检查运行时数据库和 Redis 依赖是否就绪.
        # 参数: 无.
        # 返回: 各依赖名称到就绪状态的映射.
        try:
            checks = dict(
                await check_minimum_schema_version(sessions, settings.required_schema_version)
            )
        except Exception:
            checks = {"mysql": False, "schema": False}
        try:
            checks["redis"] = bool(await redis.ping())
        except Exception:
            checks["redis"] = False
        checks["configuration"] = True
        return checks

    return Runtime(routers, readiness, engine, redis, miniapp_client, v15)


def _required_secret(value: SecretStr | None, name: str) -> str:
    # 功能: 读取必需密钥,缺失时以不泄露内容的错误中止启动.
    # 参数:
    #     value: 需要读取的受保护密钥配置,缺失时禁止构建服务.
    #     name: 缺失密钥对应的配置名称,仅用于错误提示.
    # 返回: 配置中的必需密钥明文;未配置时抛出启动错误.
    if value is None or not value.get_secret_value():
        raise ValueError(f"{name} is required")
    return value.get_secret_value()
