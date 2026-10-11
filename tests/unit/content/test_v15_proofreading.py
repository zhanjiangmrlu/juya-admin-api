from juya_admin_api.modules.content.v15_proofreading import collection_hash, sample_count


def test_sample_count_boundary_and_cap():
    assert [sample_count(n) for n in (0, 1, 20, 21, 100, 1000)] == [0, 1, 1, 2, 5, 20]


def test_collection_hash_order_stable_and_binds_each_revision():
    assert collection_hash({"b": "2", "a": "1"}) == collection_hash({"a": "1", "b": "2"})
    assert collection_hash({"a": "1"}) != collection_hash({"b": "1"})
