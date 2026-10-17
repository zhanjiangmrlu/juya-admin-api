import hashlib
import io
import wave

import pytest

from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x01\x00" * 48000)
    return stream.getvalue()


def test_real_slice_is_independent_decoded_audio():
    source = wav_bytes()
    result = FFmpegSlicer().slice(source, hashlib.sha256(source).hexdigest(), Boundary(500, 1500))
    assert result.duration_ms == pytest.approx(1000, abs=30)
    assert result.data != source
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()


def test_source_replacement_is_rejected_before_decode():
    with pytest.raises(ValueError, match="source hash"):
        FFmpegSlicer().slice(wav_bytes(), "changed", Boundary(0, 500))


@pytest.mark.parametrize("start,end", [(-1, 100), (100, 100), (200, 100)])
def test_invalid_boundaries(start, end):
    with pytest.raises(ValueError):
        Boundary(start, end)


def test_end_outside_source_rejected():
    source = wav_bytes()
    with pytest.raises(ValueError, match="source duration"):
        FFmpegSlicer().slice(source, hashlib.sha256(source).hexdigest(), Boundary(0, 4000))


def test_playlist_cannot_read_arbitrary_private_file():
    data = b"#EXTM3U\n#EXTINF:1,\nfile:///etc/passwd\n"
    with pytest.raises(ValueError):
        FFmpegSlicer().slice(data, hashlib.sha256(data).hexdigest(), Boundary(0, 500))


def test_truncated_audio_fails_full_decode():
    data = wav_bytes()[:100]
    with pytest.raises(ValueError):
        FFmpegSlicer().slice(data, hashlib.sha256(data).hexdigest(), Boundary(0, 500))
