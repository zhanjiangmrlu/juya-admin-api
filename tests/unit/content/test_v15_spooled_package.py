import hashlib
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from juya_admin_api.modules.content_import.parser import ImportLimits, SpoolFiles, read_container


def test_batch_resources_expand_to_private_disk_and_cleanup_after_use() -> None:
    data = b"independent-audio-bytes" * 50000
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("audio/one.wav", data)
        archive.writestr("audio/two.wav", data[::-1])
    files = read_container(output.getvalue(), ImportLimits(), spool=True)
    assert isinstance(files, SpoolFiles)
    directory = Path(files.temporary.name)
    assert all(path.is_file() for path in files.paths.values())
    assert hashlib.sha256(files["audio/one.wav"]).digest() == hashlib.sha256(data).digest()
    files.close()
    assert not directory.exists()
