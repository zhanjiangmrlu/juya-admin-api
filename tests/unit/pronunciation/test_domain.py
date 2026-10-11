from juya_admin_api.modules.pronunciation.domain import Config, Target, cache_key


def test_cache_separates_reading_and_every_configuration_field():
    target = Target("a", "read", "present")
    config = Config(voice=101050)
    assert cache_key(target, config) != cache_key(Target("b", "read", "past"), config)
    assert cache_key(target, config) != cache_key(target, Config(voice=101051))
    assert cache_key(target, config) == cache_key(Target("b", "read", "present"), config)


def test_disabling_new_synthesis_or_changing_price_preserves_cached_audio():
    from dataclasses import replace

    target = Target("a", "hello")
    config = Config(enabled=True, price_version="old")
    assert cache_key(target, config) == cache_key(
        target, replace(config, enabled=False, price_version="new", microcost_per_character=999)
    )
