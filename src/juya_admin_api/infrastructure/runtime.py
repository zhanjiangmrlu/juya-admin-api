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
from juya_admin_api.infrastructure.security.service_hmac import RedisNonceStore
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
from juya_admin_api.modules.contacts.router import create_contact_router
from juya_admin_api.modules.contacts.service import ContactAdminService
from juya_admin_api.modules.content.production_router import create_production_content_router
from juya_admin_api.modules.content.production_store import ProductionStore
from juya_admin_api.modules.content.repository import SQLAlchemyContentRepository
from juya_admin_api.modules.content.router import create_content_router
from juya_admin_api.modules.content.service import ContentService
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
from juya_admin_api.modules.limited_entitlements.repository import (
    SQLAlchemyLimitedEntitlementRepository,
    SQLAlchemyLimitedGrantPort,
)
from juya_admin_api.modules.limited_entitlements.router import (
    create_limited_entitlement_router,
)
from juya_admin_api.modules.limited_entitlements.service import LimitedEntitlementService
from juya_admin_api.modules.media.quota import OcrQuotaService, SQLAlchemyOcrQuotaRepository
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
from juya_admin_api.modules.system_config.service import (
    SQLAlchemySystemConfigRepository,
    SystemConfigService,
)
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

    async def close(self) -> None:
        await self.miniapp_client.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def build_runtime(settings: Settings) -> Runtime:
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
        if session_token is None:
            raise AppError("ADMIN_SESSION_INVALID", "管理员会话无效或已过期", 401)
        return await auth.authenticate_session(session_token, datetime.now(UTC))

    async def current_admin_write(
        session: Annotated[SessionRecord, Depends(current_admin)],
        csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    ) -> SessionRecord:
        auth.verify_csrf(session, csrf_token)
        return session

    config = SystemConfigService(SQLAlchemySystemConfigRepository(sessions))
    audit = AuditService(SQLAlchemyAuditRepository(sessions))
    content_repository = SQLAlchemyContentRepository(
        sessions, require_review=settings.content_security_enabled
    )
    content = ContentService(content_repository)
    production_store = ProductionStore(sessions, require_review=settings.content_security_enabled)
    formal = FormalEntitlementService(SQLAlchemyFormalEntitlementRepository(sessions))
    limited = LimitedEntitlementService(SQLAlchemyLimitedEntitlementRepository(sessions))
    entitlement_queries = SQLAlchemyEntitlementQueryRepository(sessions)
    campaigns = CampaignService(SQLAlchemyCampaignRepository(sessions))
    access = AccessPolicyService(
        content_repository,
        SQLAlchemyFormalGrantPort(sessions),
        SQLAlchemyLimitedGrantPort(sessions),
    )
    feedback = FeedbackService(SQLAlchemyFeedbackRepository(sessions))
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
    media = MediaService(
        oss,
        SQLAlchemyMediaRepository(sessions),
        signed_url_ttl_seconds=settings.signed_url_ttl_seconds,
        security=create_content_security_provider(settings, oss),
        ffprobe_path=settings.ffprobe_path,
        require_review=settings.content_security_enabled,
    )
    media_admin = MediaAdminService(SQLAlchemyMediaAdminRepository(sessions))
    media_dispatcher = CeleryMediaTaskDispatcher(
        enabled=bool(
            settings.ocr_provider == "baidu"
            and settings.baidu_ocr_api_key
            and settings.baidu_ocr_secret_key
        )
    )
    miniapp_client = MiniappApiClient(
        settings.miniapp_api_base_url,
        internal_secret.get_secret_value().encode(),
    )
    contacts = ContactAdminService(miniapp_client, audit)
    users = UserProjectionService(
        SQLAlchemyUserProjectionRepository(sessions),
        miniapp_client,
        audit,
    )
    dashboard = DashboardService(SQLAlchemyDashboardRepository(sessions))
    work_items = WorkItemService(SQLAlchemyWorkItemSource(sessions))
    analytics = SQLAlchemyAnalyticsRepository(sessions)
    deletion = DeletionCleanupService(SQLAlchemyDeletionRepository(sessions))
    routers = (
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
        create_admin_security_router(auth, config, audit_service=audit),
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
        create_entitlement_query_router(entitlement_queries, current_admin=current_admin),
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
            ocr_quota_service=OcrQuotaService(SQLAlchemyOcrQuotaRepository(sessions)),
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

    return Runtime(routers, readiness, engine, redis, miniapp_client)


def _required_secret(value: SecretStr | None, name: str) -> str:
    if value is None or not value.get_secret_value():
        raise ValueError(f"{name} is required")
    return value.get_secret_value()
