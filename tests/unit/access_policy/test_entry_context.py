from juya_admin_api.modules.access_policy.router import entry_source_context
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.text_spans import build_clickable_spans


def test_cross_line_phrase_context_contains_all_matched_sentences() -> None:
    # 功能:验证跨行短语上下文包含全部匹配句子。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    content = build_clickable_spans(
        SceneContent.model_validate(
            {
                "dialogue": [
                    {"id": "s1", "english": "put"},
                    {"id": "s2", "english": "together"},
                ],
                "chunks": [
                    {
                        "entry_id": "e1",
                        "english": "put together",
                        "source_sentence_ids": ["s1", "s2"],
                    }
                ],
            }
        )
    )
    locator = content.dialogue[0].clickable_spans[0].source_locator
    assert entry_source_context(content, content.chunks[0], locator) == "put\ntogether"


def test_list_entry_has_a_nonempty_authoritative_favorite_snapshot() -> None:
    # 功能:验证条目列表包含非空且权威的收藏快照。
    # 参数:无。
    # 返回:无;断言失败时由 pytest 报告该用例失败。
    content = SceneContent.model_validate({"vocabulary": [{"entry_id": "e1", "english": "Hello"}]})
    assert entry_source_context(content, content.vocabulary[0], "vocabulary:e1") == "Hello"
