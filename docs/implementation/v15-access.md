# V1.5 类型化内部访问与收藏确认

## 运行时装配

新增模块不覆盖既有 /internal/v1/scenes 路由。

```python
service = V15AccessService(
    sessions,
    content_loader=SQLContentLoader(short_reading_store),
    authorizer=SQLAuthorizer(),
    favorite_authority=SQLFavoriteAuthority(),
    resource_signer=resource_signer,
    credential_secret=private_runtime_secret,
)
app.include_router(create_v15_access_router(service, current_service=current_service))
```

所有 Protocol 定义在 ports.py。SQLContentLoader只接受当前已发布 schema2 场景、确认条目，或独立已发布短文，不伪造历史义项。SQLAuthorizer检查 ACTIVE 用户、模块、当前发布、正式包（typed 及旧 scene 关联）、开放场景或场景限时权益；短文使用独立 trial 和正式包，未开通时仅安全预览。模块版本取 discovery_config_state.version，授权版本固定用户更新时间、包状态、有效授权版本及试读/开放配置。

ResourceSigner(session, ResourceRequest, expires_at) 必须使用给定同一session重新核对不可变媒体、资源版本、状态、安全审核、角色和合法引用，返回短期地址及expires_at。expires_at是任一有效授权路径的边界与60秒中较早者。禁止重新接受客户端 arbitrary asset_id/version；ResourceRequest已由当前内容或BFF固定收藏生成。callback必须实际尊重expires_at；长期地址、公开整段资源或只按客户端ID取址不符合端口契约。

FavoriteAuthority(session,user_id,favorite_id) 必须查询 BFF 的权威 active owned snapshot；默认 SQLFavoriteAuthority 直接只读共享 BFF 表，检查用户ACTIVE、favorite.owner、current_snapshot_revision_id，不能用 retention projection 推测收藏存在。

## 新内部 HTTP 接口

所有新增接口使用现有 HMAC current_service 检查方法/原始路径/nonce/时间/请求body；仅配置的 juya-miniapp-api 服务允许。Body中的user_id必须由BFF从已认证请求派生，客户端不可直接选择。响应no-store。

- POST /internal/v1/content/{content_type}/{content_id}/open，body {user_id} → schema2 SceneDetail / ShortReadingDetail。
- POST .../preview，body {user_id} → 安全字段白名单，无正文、条目、原音、学习原图、精确定位。
- POST .../resources/{target_id}/signed-url，body {user_id,resource_version} → 当前授权内容固定资源短期地址。
- POST /internal/v1/favorite-snapshots/confirmation，body {user_id,reference} → {credential,entry,safe_summary}，reference为content_type/id/version及entry_id/sense_id/source_id。
- POST /internal/v1/favorite-snapshots/{favorite_id}/source-resolution，body {user_id} → EXACT / UPDATED_OR_UNRESOLVED / NO_ACCESS。无权不返回当前版本或精确定位；不按英文或位置猜配，当前同稳定来源须唯一且句ID存在。
- POST /internal/v1/favorite-snapshots/{favorite_id}/resources/{target_id}/signed-url，body {user_id,resource_version} → 本人固定 WORD_IMAGE 或 WORD/CHUNK 发音；不允许 WHOLE/SENTENCE/LEARNING_IMAGE。源权益到期、源下线或模块关闭不阻止有效收藏词语资源复习，媒体独立停用继续拒绝。
- POST /internal/v1/favorite-resource-projections，body FavoriteResourceEvent → {applied,sequence,reason?}。必须逐字段匹配BFF favorite_resource_reference权威事件；旧序号不恢复已取消引用，同序号不同载荷409，重复事件不重复创建投影。

不需要新增数据库列。只写 favorite_resource_projection；favorite_snapshot、favorite_snapshot_revision、favorite_resource_reference保持BFF唯一写入权。

## 收藏落库的跨服务窗口

credential绑定用户、类型、内容、发布版本/修订、entry/sense/source、服务器完整条目details_hash、module_version、authorization_version及不超过60秒expires_at。签名格式为 base64url(canonical JSON去掉padding) + '.' + hex HMAC-SHA256；canonical JSON用UTF-8、sort_keys=True、ensure_ascii=False、separators=(',',':')。token之外每个credential字段均在签名claims内。

verify_confirmation_within(session,credential,user_id,now)执行签名/期限/主体验证，同一session对所有现行内容、模块、用户与授权事实取得共享锁，重新核对当前条目hash和版本，再返回ConfirmedEntry。BFF写快照必须在它自己的共享MySQL事务中执行相同签名与锁事实核对，并持续持锁到唯一快照及资源引用落库提交；HTTP confirmation返回时它的事务已经结束，HTTP验证不能把锁交给另一个服务。不要把“先调用确认，再另外落库”描述为并发撤权已被排除。

SQLAuthorizer锁顺序为模块 → discovery配置 → 用户 → 内容 → 内容包（id排序）→正式权益 →开放/试读及适用限时权益。发布、模块开关、撤权等写命令应与BFF采用同一约定并对MySQL死锁作有界重试。服务只读权威收藏的共享锁会持续到资源签名事务结束。

## 测试证据与待集成

先执行缺少credentials模块的失败测试，再实现。当前8个单元通过，覆盖签名篡改/过期、预览白名单、同事务lock=True、绑定用户/模块/授权/资料hash、陈旧义项来源拒绝、失权来源无精确信息、独立收藏复习、整段/原图/旧版本拒绝、chunk无图及事件伪造。ruff/mypy通过。

tests/integration/test_v15_access.py 验证真实MySQL引用投影、重复、伪造、取消和乱序；当前无 JUYA_TEST_DATABASE_URL 而跳过，集成负责人应在隔离迁移环境执行。真实资源签名适配、跨服务BFF最终事务锁、撤权并发、云端URL实际TTL与停用窗口须集成验证后记录，不由callback fixture替代。
