import hashlib
import json
import re
import stat
import unicodedata
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any
from zipfile import BadZipFile, ZipFile

from defusedxml import ElementTree
from docx import Document
from openpyxl import load_workbook

from juya_admin_api.modules.content.v15_contracts import IMPORT_NAMESPACE, ContentType
from juya_admin_api.shared.errors import AppError


@dataclass(frozen=True)
class ImportLimits:
    max_package_bytes: int = 512 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_file_bytes: int = 50 * 1024 * 1024
    max_files: int = 12000
    max_ratio: int = 200
    max_path_length: int = 240
    max_items: int = 1000


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def safe_path(value: str, limits: ImportLimits) -> str:
    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    parts = value.split("/")
    if (
        not value
        or len(value) > limits.max_path_length
        or value.startswith("/")
        or any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in parts)
        or any(c in value for c in (":", "\x00"))
        or any(ord(c) < 32 for c in value)
        or any(
            part.split(".")[0].upper()
            in {
                "CON",
                "PRN",
                "AUX",
                "NUL",
                *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)),
            }
            for part in parts
        )
    ):
        raise AppError("IMPORT_PATH_INVALID", "包内路径不安全,请使用明确相对路径", 422)
    return value


def read_container(data: bytes, limits: ImportLimits) -> dict[str, bytes]:
    if len(data) > limits.max_package_bytes:
        raise AppError("IMPORT_LIMIT_EXCEEDED", "压缩包超过上传限制", 422)
    try:
        with ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > limits.max_files:
                raise AppError("IMPORT_LIMIT_EXCEEDED", "压缩包文件数量超过限制", 422)
            paths: dict[str, str] = {}
            for info in infos:
                path = safe_path(
                    info.filename.rstrip("/") if info.is_dir() else info.filename, limits
                )
                if (
                    path.casefold() in paths
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 1
                ):
                    raise AppError("IMPORT_PATH_INVALID", "压缩包包含重复路径、链接或加密文件", 422)
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise AppError("IMPORT_PATH_INVALID", "压缩包不能包含设备或特殊文件", 422)
                paths[path.casefold()] = path
                if (
                    info.file_size > limits.max_file_bytes
                    or info.file_size > max(1, info.compress_size) * limits.max_ratio
                ):
                    raise AppError("IMPORT_LIMIT_EXCEEDED", "文件展开规模或压缩比超过限制", 422)
            files: dict[str, bytes] = {}
            total = 0
            for info in infos:
                if info.is_dir():
                    continue
                chunks: list[bytes] = []
                count = 0
                with archive.open(info) as stream:
                    while block := stream.read(65536):
                        count += len(block)
                        total += len(block)
                        if (
                            count > limits.max_file_bytes
                            or total > limits.max_total_bytes
                            or count > max(1, info.compress_size) * limits.max_ratio
                        ):
                            raise AppError("IMPORT_LIMIT_EXCEEDED", "实际展开字节超过限制", 422)
                        chunks.append(block)
                files[safe_path(info.filename, limits)] = b"".join(chunks)
            return files
    except (BadZipFile, RuntimeError, OSError, EOFError) as error:
        raise AppError("IMPORT_CONTAINER_INVALID", "压缩文件损坏,无法解析", 422) from error


def validate_ooxml(data: bytes, limits: ImportLimits) -> None:
    files = read_container(data, limits)
    for path, body in files.items():
        if "vbaproject" in path.casefold() or path.endswith((".exe", ".dll")):
            raise AppError("IMPORT_DOCUMENT_UNSAFE", "文档不能包含宏或可执行附件", 422)
        if path.endswith((".xml", ".rels")):
            try:
                tree = ElementTree.fromstring(body)
                if path.endswith(".rels") and any(
                    e.get("TargetMode", "").casefold() == "external" for e in tree
                ):
                    raise AppError("IMPORT_DOCUMENT_UNSAFE", "文档不能包含外部关系", 422)
            except AppError:
                raise
            except Exception as error:
                raise AppError(
                    "IMPORT_DOCUMENT_UNSAFE", "文档XML包含不安全或损坏内容", 422
                ) from error


def read_sheet(data: bytes, limits: ImportLimits) -> list[dict[str, Any]]:
    validate_ooxml(data, limits)
    try:
        book = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
        try:
            rows = iter(book.worksheets[0].iter_rows())
            headings = [str(c.value or "").strip() for c in next(rows)]
            if len(headings) != len(set(headings)):
                raise ValueError("duplicate columns")
            result: list[dict[str, Any]] = []
            for number, cells in enumerate(rows, start=2):
                if number > limits.max_items * 100 + 2:
                    raise AppError("IMPORT_LIMIT_EXCEEDED", "表格行数超过限制", 422)
                row: dict[str, Any] = {
                    head: str(cell.value).strip() if cell.value is not None else ""
                    for head, cell in zip(headings, cells, strict=False)
                    if head
                }
                if not any(row.values()):
                    continue
                row["row_no"] = number
                row["formula_columns"] = [
                    head
                    for head, cell in zip(headings, cells, strict=False)
                    if cell.data_type == "f"
                ]
                result.append(row)
            return result
        finally:
            book.close()
    except AppError:
        raise
    except Exception as error:
        raise AppError("IMPORT_XLSX_INVALID", "内容表无法解析或列名重复", 422) from error


def parse_docx(data: bytes, kind: ContentType, limits: ImportLimits) -> list[dict[str, Any]]:
    validate_ooxml(data, limits)
    try:
        doc = Document(BytesIO(data))
    except Exception as error:
        raise AppError("IMPORT_DOCX_INVALID", "双语DOCX无法解析", 422) from error
    articles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    mode = "body"
    for p in doc.paragraphs:
        text = p.text.strip().replace("\r\n", "\n")
        if not text:
            continue
        if (p.style.name if p.style else "").startswith(("Heading", "标题")) and (
            "\uff5c" in text or "|" in text
        ):
            titles = re.split(r"[\uff5c|]", text, maxsplit=1)
            current = {
                "title_zh": titles[0].strip(),
                "title_en": titles[1].strip(),
                "english": [],
                "chinese": [],
                "copyright": "",
                "source": "",
            }
            articles.append(current)
            mode = "body"
            continue
        if current is None:
            current = {
                "title_en": "",
                "title_zh": "",
                "english": [],
                "chinese": [],
                "copyright": "",
                "source": "",
            }
            articles.append(current)
        if text.casefold() in {"英文正文", "english", "english text"}:
            mode = "english"
        elif text.casefold() in {"中文译文", "全文中文译文", "中文正文", "translation", "chinese"}:
            mode = "chinese"
        elif text.casefold() in {"vocabulary", "useful chunks", "重点词汇", "词汇", "语块"}:
            mode = "attachments"
        elif text.startswith(("来源\uff1a", "Source:")):
            current["source"] = re.split(r"[:\uff1a]", text, maxsplit=1)[1].strip()
        elif text.startswith(("版权\uff1a", "Copyright:")):
            current["copyright"] = re.split(r"[:\uff1a]", text, maxsplit=1)[1].strip()
        elif mode != "attachments":
            lang = (
                mode
                if mode in {"english", "chinese"}
                else "chinese"
                if re.search(r"[\u4e00-\u9fff]", text)
                else "english"
            )
            current[lang].append(text)
    results = []
    for article in articles:
        candidate = {k: article[k] for k in ("title_en", "title_zh", "source", "copyright")}
        if kind == "short_reading":
            candidate.update(
                paragraphs_en=article["english"], translation_zh="\n\n".join(article["chinese"])
            )
        else:
            dialogue = []
            for index, en in enumerate(article["english"]):
                match = re.match(r"^([^:\uff1a]{1,100})[:\uff1a]\s*(.+)$", en)
                chinese = article["chinese"][index] if index < len(article["chinese"]) else ""
                zh_match = re.match(r"^([^:\uff1a]{1,100})[:\uff1a]\s*(.+)$", chinese)
                dialogue.append(
                    {
                        "speaker": match[1] if match else "",
                        "english": match[2] if match else en,
                        "chinese": zh_match[2] if zh_match else chinese,
                    }
                )
            candidate.update(schema_version=2, dialogue=dialogue, vocabulary=[], chunks=[])
        results.append(candidate)
    return results


def parse_package(
    data: bytes, kind: ContentType, *, limits: ImportLimits | None = None
) -> dict[str, Any]:
    limits = limits or ImportLimits()
    files = read_container(data, limits)
    content_table = files.get("内容表.xlsx") or files.get("content.xlsx")
    if not content_table:
        raise AppError("IMPORT_CONTENT_TABLE_REQUIRED", "缺少内容表.xlsx", 422)
    rows = read_sheet(content_table, limits)
    if len(rows) > limits.max_items:
        raise AppError("IMPORT_LIMIT_EXCEEDED", "单逻辑批次最多1000篇", 422)
    keys = Counter(row.get("import_content_id", "") for row in rows)
    word_rows = read_sheet(files["词汇表.xlsx"], limits) if "词汇表.xlsx" in files else []
    word_keys = Counter(
        (r.get("import_content_id", ""), r.get("import_entry_id") or r.get("entry_id", ""))
        for r in word_rows
    )
    words_by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for word in word_rows:
        words_by_parent[word.get("import_content_id", "")].append(word)
    referenced = {"内容表.xlsx", "content.xlsx", "词汇表.xlsx"}
    items = []
    for row in rows:
        issues: list[dict[str, Any]] = []

        def issue(
            code: str,
            field: str,
            message: str,
            severity: str = "ERROR",
            *,
            target: list[dict[str, Any]] = issues,
            row_no: int = row["row_no"],
        ) -> None:
            target.append(
                {
                    "code": code,
                    "field": field,
                    "message": message,
                    "severity": severity,
                    "row_no": row_no,
                }
            )

        key = row.get("import_content_id", "")
        if not key or len(key) > 64:
            issue("IMPORT_ID_REQUIRED", "import_content_id", "填写稳定内容导入ID,长度不超过64")
        if key and keys[key] > 1:
            issue("IMPORT_DUPLICATE_ID", "import_content_id", "同批内容ID重复,请修正或排除")
        if row["formula_columns"]:
            issue("IMPORT_FORMULA_INVALID", "formula_columns", "公式不计算,请转为明确字面值")
        bound: dict[str, dict[str, str]] = {}
        for field in ("document_path", "cover_path", "learning_image_path", "audio_path"):
            path = row.get(field, "")
            if not path:
                if field == "document_path":
                    issue("IMPORT_DOCUMENT_REQUIRED", field, "每篇需一个双语DOCX")
                elif field == "audio_path" and kind == "dialogue":
                    issue("AUDIO_MISSING", field, "场景发布前需完整对话音频", "DRAFT")
                continue
            path = safe_path(path, limits)
            referenced.add(path)
            ext = path.rsplit(".", 1)[-1].casefold()
            allowed = {
                "document_path": {"docx"},
                "cover_path": {"png", "jpg", "jpeg", "webp"},
                "learning_image_path": {"png", "jpg", "jpeg", "webp"},
                "audio_path": {"mp3", "m4a", "wav", "aac"},
            }[field]
            if path not in files or ext not in allowed:
                issue("IMPORT_RESOURCE_INVALID", field, "路径不存在或文件类型不符")
            elif kind == "short_reading" and field == "learning_image_path":
                issue("IMPORT_ROLE_INVALID", field, "纯阅读短文不配置受限学习原图")
            else:
                bound[field] = {
                    "path": path,
                    "sha256": hashlib.sha256(files[path]).hexdigest(),
                    "role": field,
                }
        candidate: dict[str, Any] = {}
        if "document_path" in bound:
            try:
                articles = parse_docx(files[bound["document_path"]["path"]], kind, limits)
                if len(articles) != 1:
                    issue(
                        "IMPORT_DOCUMENT_BOUNDARY_INVALID",
                        "document_path",
                        "统一ZIP每行DOCX必须恰好一篇",
                    )
                elif articles:
                    candidate = articles[0]
            except AppError as error:
                issue(error.code, "document_path", "文档损坏或包含不安全资料,请重新保存")
        for field in (
            "title_en",
            "title_zh",
            "category",
            "safe_intro",
            "topic",
            "level",
            "source_copyright",
        ):
            value = row.get(field, "")
            if not value:
                continue
            if (
                field in {"title_en", "title_zh"}
                and candidate.get(field)
                and value != candidate[field]
            ):
                issue("IMPORT_TEXT_CONFLICT", field, "表格与文档标题不同,需明确确认", "CONFIRM")
            candidate[field if field != "source_copyright" else "copyright"] = value
        if kind == "short_reading":
            candidate.setdefault("category", "")
            for field in ("safe_intro", "topic", "level"):
                candidate.setdefault(field, "")
        elif candidate.get("dialogue"):
            if any(not s["chinese"] for s in candidate["dialogue"]):
                issue(
                    "BILINGUAL_PAIRING_REQUIRED",
                    "document_path",
                    "缺少对应中文或配对不清,需校对",
                    "DRAFT",
                )
        entries = []
        for word in words_by_parent.get(key, []) if kind != "short_reading" else []:
            if word.get("import_content_id") != key:
                continue
            word_key = word.get("import_entry_id") or word.get("entry_id", "")
            item = {k: v for k, v in word.items() if k not in {"formula_columns", "row_no"}}
            item["import_entry_id"] = word_key
            if not word_key or word_keys[(key, word_key)] > 1:
                issue("IMPORT_DUPLICATE_ENTRY_ID", "import_entry_id", "同来源条目ID为空或重复")
            if word["formula_columns"]:
                issue("IMPORT_FORMULA_INVALID", "import_entry_id", "条目公式需转为字面值")
            if word.get("entry_type") not in {"word", "chunk"}:
                issue("ENTRY_TYPE_INVALID", "entry_type", "条目类型须为word或chunk")
            if word.get("entry_type") == "chunk" and word.get("image_path"):
                issue("CHUNK_IMAGE_INVALID", "image_path", "语块不接收图片")
            for field in ("image_path", "pronunciation_path"):
                if not word.get(field):
                    continue
                path = safe_path(word[field], limits)
                referenced.add(path)
                if path not in files:
                    issue("IMPORT_RESOURCE_INVALID", field, "条目素材路径不存在")
                else:
                    allowed = (
                        {".png", ".jpg", ".jpeg", ".webp"}
                        if field == "image_path"
                        else {".mp3", ".wav", ".m4a", ".aac"}
                    )
                    if PurePosixPath(path).suffix.lower() not in allowed:
                        issue(
                            "IMPORT_RESOURCE_EXTENSION_INVALID", field, "素材扩展名不符合角色要求"
                        )
                    item[field + "_hash"] = hashlib.sha256(files[path]).hexdigest()
            entries.append(item)
        defaults = ["default_cover"] if not row.get("cover_path") else []
        defaults += [
            "no_image:" + e["import_entry_id"]
            for e in entries
            if e.get("entry_type") == "word" and not e.get("image_path")
        ]
        input_hash = canonical_hash(
            {
                "rules": "v15.1",
                "type": kind,
                "candidate": candidate,
                "resources": bound,
                "entries": entries,
            }
        )
        items.append(
            {
                "row_no": row["row_no"],
                "import_content_id": key,
                "candidate": candidate,
                "entries": entries,
                "resources": bound,
                "issues": issues,
                "defaults": defaults,
                "input_hash": input_hash,
                "expected_content_version": row.get("expected_content_version") or None,
            }
        )
    orphan_rows = [
        r
        for r in word_rows
        if not r.get("import_content_id") or r.get("import_content_id") not in keys
    ]
    orphans = [r["row_no"] for r in orphan_rows]
    batch_issues = [
        {
            "code": "IMPORT_ORPHAN_ENTRY",
            "severity": "ERROR",
            "row_no": r["row_no"],
            "field": "import_content_id",
            "import_content_id": r.get("import_content_id", ""),
            "message": "词汇表来源内容ID为空或不在内容表,请修正后重新上传",
        }
        for r in orphan_rows
    ]
    if kind == "short_reading" and word_rows:
        batch_issues.append(
            {
                "code": "IMPORT_READING_ENTRIES_FORBIDDEN",
                "severity": "ERROR",
                "field": "词汇表.xlsx",
                "message": "纯阅读短文不接收词条附件",
            }
        )
    return {
        "namespace": IMPORT_NAMESPACE,
        "parser_version": "v15.1",
        "content_type": kind,
        "original_sha256": hashlib.sha256(data).hexdigest(),
        "items": items,
        "unreferenced_files": sorted(set(files) - referenced),
        "orphan_entry_rows": orphans,
        "issues": batch_issues,
        "limits": asdict(limits),
    }
