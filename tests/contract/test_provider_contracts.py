from typing import get_type_hints

from juya_admin_api.integrations.content_security.protocol import ContentSecurityProvider
from juya_admin_api.integrations.ocr.protocol import OcrProvider
from juya_admin_api.integrations.oss.provider import OssProvider
from juya_admin_api.integrations.tts.protocol import TtsProvider


def test_external_provider_protocols_expose_stable_adapter_methods() -> None:
    assert set(OssProvider.__dict__) >= {
        "create_upload_policy",
        "head_object",
        "sign_get_url",
        "delete_object",
    }
    assert set(OcrProvider.__dict__) >= {"recognize"}
    assert set(TtsProvider.__dict__) >= {"synthesize"}
    assert set(ContentSecurityProvider.__dict__) >= {"scan_text", "scan_image"}
    assert get_type_hints(OssProvider.sign_get_url)["expires_in"] is int
