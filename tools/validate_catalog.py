#!/usr/bin/env python3
"""Prepare and validate a public textbook library catalog."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit

import jsonschema
import pypdfium2 as pdfium
from PIL import Image


def find_library_root() -> Path:
    script_dir = Path(__file__).resolve().parent
    for candidate in (script_dir, *script_dir.parents):
        if (candidate / "catalog.json").is_file() and (
            candidate / "catalog.schema.json"
        ).is_file():
            return candidate
    raise RuntimeError("找不到 catalog.json 与 catalog.schema.json")


ROOT = find_library_root()
CATALOG_PATH = ROOT / "catalog.json"
SCHEMA_PATH = ROOT / "catalog.schema.json"
MAX_WARNING_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_COVER_BYTES = 2 * 1024 * 1024
MAX_COVER_SIDE = 1024


class CatalogError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_file(root: Path, raw_path: str) -> tuple[Path, str]:
    if not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise CatalogError(f"路径无效：{raw_path!r}")
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise CatalogError(f"路径必须是仓库内的相对路径：{raw_path}")
    candidate = root.joinpath(*relative.parts)
    try:
        candidate.resolve(strict=True).relative_to(root.resolve())
    except (OSError, ValueError) as error:
        raise CatalogError(f"路径不存在或越界：{raw_path}") from error
    if not candidate.is_file():
        raise CatalogError(f"路径不是文件：{raw_path}")
    return candidate, relative.as_posix()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CatalogError(f"git {' '.join(args)} 失败：{detail}")
    return result.stdout.strip()


def tree_blob(root: Path, commit: str, relative: str) -> str:
    blob = git(root, "rev-parse", f"{commit}:{relative}")
    if len(blob) != 40 or git(root, "cat-file", "-t", blob) != "blob":
        raise CatalogError(f"Git commit 中找不到文件 blob：{relative}")
    return blob


def worktree_blob(root: Path, relative: str) -> str:
    return git(root, "hash-object", relative)


def pdf_info(path: Path) -> tuple[str, int, int, dict[str, int | bool]]:
    try:
        with pdfium.PdfDocument(str(path)) as document:
            page_count = len(document)
            pages_with_text = 0
            character_count = 0
            for index in range(page_count):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        text = text_page.get_text_range()
                    finally:
                        text_page.close()
                finally:
                    page.close()
                character_count += len(text)
                if text.strip():
                    pages_with_text += 1
    except Exception as error:
        raise CatalogError(f"PDFium 无法打开 {path.name}：{error}") from error
    if page_count < 1:
        raise CatalogError(f"PDF 没有页面：{path.name}")
    return (
        sha256_file(path),
        path.stat().st_size,
        page_count,
        {
            "hasTextLayer": pages_with_text > 0,
            "pagesWithText": pages_with_text,
            "characterCount": character_count,
        },
    )


def render_cover(pdf_path: Path, cover_path: Path) -> None:
    try:
        with pdfium.PdfDocument(str(pdf_path)) as document:
            if len(document) < 1:
                raise CatalogError(f"PDF 没有页面：{pdf_path.name}")
            page = document[0]
            try:
                width, height = page.get_size()
                if width <= 0 or height <= 0:
                    raise CatalogError(f"PDF 首页尺寸无效：{pdf_path.name}")
                scale = min(1.0, 768 / max(width, height))
                image = page.render(
                    scale=scale,
                    fill_color=(255, 255, 255, 255),
                ).to_pil()
            finally:
                page.close()
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(cover_path, format="WEBP", quality=84, method=6)
    except CatalogError:
        raise
    except Exception as error:
        raise CatalogError(f"无法生成封面 {cover_path.name}：{error}") from error


def load_catalog(root: Path = ROOT) -> dict:
    path = root / "catalog.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"无法读取 {path}: {error}") from error


def write_catalog(root: Path, catalog: dict) -> None:
    path = root / "catalog.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def canonical_pdf_url(repository: str, commit: str, path: str) -> str:
    encoded = quote(f"{repository}/{commit}/{path}", safe="/")
    return f"https://raw.githubusercontent.com/{encoded}"


def expected_book_directory(book: dict) -> str:
    semester = {"上": "upper", "下": "lower"}.get(book["semester"])
    if semester is None:
        raise CatalogError(f"学期无效：{book['editionId']}")
    return (
        f"books/{book['subject']}/grade-{book['grade']:02d}/"
        f"{semester}"
    )


def validate_source_path(book: dict, source_path: str) -> None:
    path = PurePosixPath(source_path)
    if (
        path.parent.as_posix() != expected_book_directory(book)
        or path.suffix.lower() != ".pdf"
        or path.name == ".pdf"
    ):
        raise CatalogError(
            f"sourcePath 必须位于学科/年级/学期目录且为 PDF：{source_path}"
        )


def pin_book(root: Path, repository: str, book: dict) -> dict:
    pinned = dict(book)
    source_path = pinned.get("sourcePath")
    cover_path = pinned.get("coverPath")
    if not isinstance(source_path, str) or not isinstance(cover_path, str):
        raise CatalogError("每条记录必须先填写 sourcePath 和 coverPath")
    pdf, source_path = safe_file(root, source_path)
    cover, cover_path = safe_file(root, cover_path)
    validate_source_path(pinned, source_path)
    pdf_sha, file_size, page_count, pdf_text = pdf_info(pdf)
    if file_size >= MAX_FILE_BYTES:
        raise CatalogError(f"PDF 达到或超过 100 MiB，常规 Git 不支持：{source_path}")
    cover_sha = sha256_file(cover)
    if cover_path != f"covers/{pdf_sha}.webp":
        raise CatalogError(f"coverPath 必须使用 PDF SHA 命名：covers/{pdf_sha}.webp")

    # Pin both assets to one immutable tree snapshot, even if they were added in
    # separate commits. This avoids requiring every book to be recommitted.
    source_commit = git(root, "rev-parse", "HEAD")
    if len(source_commit) != 40:
        raise CatalogError("无法取得教材资源 source commit")
    pdf_blob = tree_blob(root, source_commit, source_path)
    cover_blob = tree_blob(root, source_commit, cover_path)
    if worktree_blob(root, source_path) != pdf_blob:
        raise CatalogError(f"PDF 工作区内容与已提交 commit 不同：{source_path}")
    if worktree_blob(root, cover_path) != cover_blob:
        raise CatalogError(f"封面工作区内容与已提交 commit 不同：{cover_path}")

    pinned.update(
        {
            "sha256": pdf_sha,
            "bytes": file_size,
            "pageCount": page_count,
            "pdfText": pdf_text,
            "downloadUrl": canonical_pdf_url(repository, source_commit, source_path),
            "sourceRepository": repository,
            "sourceCommit": source_commit,
            "sourcePath": source_path,
            "gitBlobSha": pdf_blob,
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "evidencePdfPages": sorted(set(pinned.get("evidencePdfPages", [1]))),
            "coverPath": cover_path,
            "coverSha256": cover_sha,
            "coverSourcePdfPage": 1,
        }
    )
    return pinned


def pin_catalog(root: Path = ROOT) -> None:
    catalog = load_catalog(root)
    repository = catalog.get("repository")
    if not isinstance(repository, str) or "/" not in repository:
        raise CatalogError("catalog.repository 必须是 owner/repository")
    updated = json.loads(json.dumps(catalog))
    updated["books"] = [pin_book(root, repository, book) for book in updated["books"]]
    write_catalog(root, updated)


def validate_catalog(root: Path = ROOT) -> list[str]:
    return validate_catalog_data(root, load_catalog(root))


def validate_catalog_data(root: Path, catalog: dict) -> list[str]:
    schema = json.loads((root / "catalog.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.validate(
            catalog,
            schema,
            format_checker=jsonschema.FormatChecker(),
        )
    except jsonschema.ValidationError as error:
        raise CatalogError(f"catalog.json schema 错误：{error.message}") from error
    except jsonschema.SchemaError as error:
        raise CatalogError(f"catalog.schema.json 无效：{error.message}") from error

    warnings: list[str] = []
    seen_sha: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    repository = catalog["repository"]
    for book in catalog["books"]:
        book_id = book["editionId"]
        identity = (book["subject"], book_id)
        if identity in seen_ids:
            raise CatalogError(f"版次 ID 重复：{book_id}")
        seen_ids.add(identity)
        if book["sha256"] in seen_sha:
            raise CatalogError(f"PDF SHA-256 重复：{book['sha256']}")
        seen_sha.add(book["sha256"])
        if book["sourceRepository"] != repository:
            raise CatalogError(f"来源仓库与 catalog.repository 不一致：{book_id}")
        validate_source_path(book, book["sourcePath"])
        if book["coverPath"] != f"covers/{book['sha256']}.webp":
            raise CatalogError(f"coverPath 与 PDF SHA 不匹配：{book_id}")
        pdf, source_path = safe_file(root, book["sourcePath"])
        cover, cover_path = safe_file(root, book["coverPath"])
        file_size = pdf.stat().st_size
        if file_size >= MAX_FILE_BYTES:
            raise CatalogError(f"PDF 达到或超过 100 MiB，常规 Git 不支持：{source_path}")
        if file_size > MAX_WARNING_BYTES:
            warnings.append(f"{source_path} 超过 50 MiB，GitHub 会显示大文件警告")
        pdf_sha, actual_bytes, page_count, pdf_text = pdf_info(pdf)
        if actual_bytes != book["bytes"]:
            raise CatalogError(f"PDF 字节数不匹配：{source_path}")
        if pdf_sha != book["sha256"]:
            raise CatalogError(f"PDF SHA-256 不匹配：{source_path}")
        if page_count != book["pageCount"]:
            raise CatalogError(f"PDF 页数不匹配：{source_path}")
        if pdf_text != book["pdfText"]:
            raise CatalogError(f"PDF 可提取文字统计不匹配：{source_path}")
        if any(page > page_count for page in book["evidencePdfPages"]):
            raise CatalogError(f"版次证据页超出 PDF 页数：{source_path}")
        if cover.stat().st_size > MAX_COVER_BYTES:
            raise CatalogError(f"封面超过 2 MiB：{cover_path}")
        if sha256_file(cover) != book["coverSha256"]:
            raise CatalogError(f"封面 SHA-256 不匹配：{cover_path}")
        try:
            with Image.open(cover) as image:
                if image.format != "WEBP" or max(image.size) > MAX_COVER_SIDE:
                    raise CatalogError(f"封面必须是最长边不超过 1024px 的 WebP：{cover_path}")
                image.verify()
        except CatalogError:
            raise
        except Exception as error:
            raise CatalogError(f"封面无法解码：{cover_path}: {error}") from error

        commit = book["sourceCommit"]
        blob = tree_blob(root, commit, source_path)
        if blob != book["gitBlobSha"] or worktree_blob(root, source_path) != blob:
            raise CatalogError(f"Git blob SHA 不匹配：{source_path}")
        cover_blob = tree_blob(root, commit, cover_path)
        if worktree_blob(root, cover_path) != cover_blob:
            raise CatalogError(f"封面不在 sourceCommit 中：{cover_path}")
        if book["downloadUrl"] != canonical_pdf_url(repository, commit, source_path):
            raise CatalogError(f"downloadUrl 不是同仓库固定 commit/path：{book_id}")
        parsed = urlsplit(book["downloadUrl"])
        if parsed.scheme != "https" or parsed.hostname != "raw.githubusercontent.com":
            raise CatalogError(f"downloadUrl 必须使用 HTTPS GitHub Raw：{book_id}")
    return warnings


def prepare_pdf(root: Path, raw_pdf: str) -> dict:
    pdf, relative = safe_file(root, raw_pdf)
    if not relative.startswith("books/") or not relative.endswith(".pdf"):
        raise CatalogError("PDF 必须位于 books/ 目录且使用 .pdf 扩展名")
    pdf_sha, file_size, page_count, pdf_text = pdf_info(pdf)
    if file_size >= MAX_FILE_BYTES:
        raise CatalogError(f"PDF 达到或超过 100 MiB，常规 Git 不支持：{relative}")
    cover_path = root / "covers" / f"{pdf_sha}.webp"
    render_cover(pdf, cover_path)
    result = {
        "sha256": pdf_sha,
        "bytes": file_size,
        "pageCount": page_count,
        "pdfText": pdf_text,
        "evidencePdfPages": [1],
        "coverPath": cover_path.relative_to(root).as_posix(),
        "coverSha256": sha256_file(cover_path),
        "coverSourcePdfPage": 1,
    }
    if file_size > MAX_WARNING_BYTES:
        print("警告：该文件超过 50 MiB，GitHub 会显示大文件警告。", file=sys.stderr)
    return result


def _has_staged_changes(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return True
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CatalogError(f"无法检查 Git 暂存区：{detail}")
    return False


def _commit_assets(root: Path, paths: list[str], edition_id: str, *, action: str = "Add") -> bool:
    git(root, "add", "-A", "--", *paths)
    if not _has_staged_changes(root):
        return False
    git(root, "commit", "-m", f"{action} textbook assets: {edition_id}")
    return True


def add_book(
    root: Path,
    *,
    pdf_path: str,
    subject: str,
    grade: int,
    semester: str,
    edition_id: str,
    title: str,
    publisher: str,
    edition_label: str,
    edition_notes: str,
    redistribution_evidence: str = "",
    public_redistribution_confirmed: bool = False,
    curriculum_standard: str | None = None,
    approval_year: int | None = None,
    publication_date: str | None = None,
    edition_number: int | None = None,
    printing_date: str | None = None,
    printing_number: int | None = None,
    evidence_pdf_pages: list[int] | None = None,
) -> tuple[dict, bool, list[str]]:
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", subject):
        raise CatalogError("subject 必须是小写 ASCII 标识符")
    if not 1 <= grade <= 12:
        raise CatalogError("grade 必须在 1 到 12 之间")
    if semester not in {"upper", "lower"}:
        raise CatalogError("semester 必须是 upper 或 lower")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", edition_id):
        raise CatalogError("edition-id 必须是小写 ASCII 标识符")
    for name, value in (
        ("title", title),
        ("publisher", publisher),
        ("edition-label", edition_label),
        ("edition-notes", edition_notes),
    ):
        if not isinstance(value, str) or not value.strip():
            raise CatalogError(f"{name} 不能为空")
    if approval_year is not None and not 1900 <= approval_year <= 2099:
        raise CatalogError("approval-year 必须在 1900 到 2099 之间")
    if edition_number is not None and edition_number < 1:
        raise CatalogError("edition-number 必须大于等于 1")
    if printing_number is not None and printing_number < 1:
        raise CatalogError("printing-number 必须大于等于 1")
    for name, value in (
        ("curriculum-standard", curriculum_standard),
        ("publication-date", publication_date),
        ("printing-date", printing_date),
    ):
        if value is not None and not value.strip():
            raise CatalogError(f"{name} 为空时请省略该参数")
    if _has_staged_changes(root):
        raise CatalogError("Git 暂存区已有其他改动；请先提交或取消暂存后重试")
    if not git(root, "config", "user.name") or not git(root, "config", "user.email"):
        raise CatalogError("请先配置本地 Git user.name 与 user.email")

    catalog = load_catalog(root)
    validate_catalog_data(root, catalog)
    repository = catalog.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise CatalogError("catalog.repository 必须是 owner/repository")
    if any(
        book.get("subject") == subject and book.get("editionId") == edition_id
        for book in catalog["books"]
    ):
        raise CatalogError(f"该学科版次已登记：{edition_id}")

    source_pdf, source_path = safe_file(root, pdf_path)
    if not source_path.startswith("books/") or source_pdf.suffix.lower() != ".pdf":
        raise CatalogError("PDF 必须来自书库 books/ 目录")
    pdf_sha, file_size, page_count, _ = pdf_info(source_pdf)
    if file_size >= MAX_FILE_BYTES:
        raise CatalogError(f"PDF 达到或超过 100 MiB，常规 Git 不支持：{source_path}")
    pages = sorted(set(evidence_pdf_pages or [1]))
    if not pages or any(
        isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > page_count
        for page in pages
    ):
        raise CatalogError("evidence-page 必须是 PDF 范围内的正整数")
    if any(book.get("sha256") == pdf_sha for book in catalog["books"]):
        raise CatalogError(f"相同 PDF SHA-256 已登记：{pdf_sha}")

    semester_label = "上" if semester == "upper" else "下"
    book = {
        "subject": subject,
        "grade": grade,
        "semester": semester_label,
        "title": title.strip(),
        "publisher": publisher.strip(),
        "editionId": edition_id,
        "curriculumStandard": curriculum_standard,
        "approvalYear": approval_year,
        "editionLabel": edition_label.strip(),
        "editionNotes": edition_notes.strip(),
        "publicationDate": publication_date,
        "editionNumber": edition_number,
        "printingDate": printing_date,
        "printingNumber": printing_number,
        "downloadable": True,
        "redistributionPermissionVerified": (
            public_redistribution_confirmed is True and bool(redistribution_evidence.strip())
        ),
        "redistributionEvidence": redistribution_evidence.strip(),
        "evidencePdfPages": pages,
    }
    validate_source_path(book, source_path)

    prepared = prepare_pdf(root, source_path)
    if prepared["sha256"] != pdf_sha:
        raise CatalogError("PDF 在整理过程中发生变化")
    book.update(prepared)

    asset_paths = [source_path, prepared["coverPath"]]
    assets_committed = _commit_assets(root, asset_paths, edition_id)
    book["sourcePath"] = source_path
    pinned = pin_book(root, repository, book)
    candidate = json.loads(json.dumps(catalog))
    candidate["books"].append(pinned)
    warnings = validate_catalog_data(root, candidate)
    write_catalog(root, candidate)
    return pinned, assets_committed, warnings


def update_book(
    root: Path,
    *,
    pdf_path: str,
    subject: str,
    grade: int,
    semester: str,
    edition_id: str,
    title: str,
    publisher: str,
    edition_label: str,
    edition_notes: str,
    redistribution_evidence: str = "",
    public_redistribution_confirmed: bool = False,
    curriculum_standard: str | None = None,
    approval_year: int | None = None,
    publication_date: str | None = None,
    edition_number: int | None = None,
    printing_date: str | None = None,
    printing_number: int | None = None,
    evidence_pdf_pages: list[int] | None = None,
    pending_update_paths: list[str] | None = None,
) -> tuple[dict, bool, list[str]]:
    """Replace one catalog entry's PDF and metadata at its existing source path."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", subject):
        raise CatalogError("subject 必须是小写 ASCII 标识符")
    if not 1 <= grade <= 12:
        raise CatalogError("grade 必须在 1 到 12 之间")
    if semester not in {"upper", "lower"}:
        raise CatalogError("semester 必须是 upper 或 lower")
    for name, value in (
        ("title", title),
        ("publisher", publisher),
        ("edition-label", edition_label),
        ("edition-notes", edition_notes),
    ):
        if not isinstance(value, str) or not value.strip():
            raise CatalogError(f"{name} 不能为空")
    if approval_year is not None and not 1900 <= approval_year <= 2099:
        raise CatalogError("approval-year 必须在 1900 到 2099 之间")
    if edition_number is not None and edition_number < 1:
        raise CatalogError("edition-number 必须大于等于 1")
    if printing_number is not None and printing_number < 1:
        raise CatalogError("printing-number 必须大于等于 1")
    for name, value in (
        ("curriculum-standard", curriculum_standard),
        ("publication-date", publication_date),
        ("printing-date", printing_date),
    ):
        if value is not None and not value.strip():
            raise CatalogError(f"{name} 为空时请省略该参数")
    if _has_staged_changes(root):
        raise CatalogError("Git 暂存区已有其他改动；请先提交或取消暂存后重试")
    if not git(root, "config", "user.name") or not git(root, "config", "user.email"):
        raise CatalogError("请先配置本地 Git user.name 与 user.email")

    catalog = load_catalog(root)
    repository = catalog.get("repository")
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise CatalogError("catalog.repository 必须是 owner/repository")
    matches = [
        (index, book)
        for index, book in enumerate(catalog["books"])
        if book.get("sourcePath") == pdf_path
    ]
    if len(matches) != 1:
        raise CatalogError(f"必须通过当前 sourcePath 唯一找到一条记录：{pdf_path}")
    book_index, previous = matches[0]
    if previous.get("editionId") != edition_id:
        raise CatalogError("更新不能更换 editionId；请保持原记录 ID")
    validate_source_path(previous, pdf_path)
    if previous.get("coverPath") != f"covers/{previous.get('sha256')}.webp":
        raise CatalogError("原目录封面路径与 PDF SHA 不匹配，拒绝更新")
    if (
        previous.get("subject") != subject
        or previous.get("grade") != grade
        or previous.get("semester") != ("上" if semester == "upper" else "下")
    ):
        raise CatalogError("更新必须保留原书目的学科、年级和学期目录")

    pending_paths = set(pending_update_paths or [])
    pending_paths.discard(pdf_path)
    remaining = json.loads(json.dumps(catalog))
    remaining["books"] = [
        item
        for item in remaining["books"]
        if item.get("sourcePath") != pdf_path
        and item.get("sourcePath") not in pending_paths
    ]
    validate_catalog_data(root, remaining)

    source_pdf, source_path = safe_file(root, pdf_path)
    if not source_path.startswith("books/") or source_pdf.suffix.lower() != ".pdf":
        raise CatalogError("PDF 必须来自书库 books/ 目录")
    pdf_sha, file_size, page_count, _ = pdf_info(source_pdf)
    if file_size >= MAX_FILE_BYTES:
        raise CatalogError(f"PDF 达到或超过 100 MiB，常规 Git 不支持：{source_path}")
    if any(book.get("sha256") == pdf_sha for book in remaining["books"]):
        raise CatalogError(f"相同 PDF SHA-256 已登记：{pdf_sha}")
    pages = sorted(set(evidence_pdf_pages or [1]))
    if not pages or any(
        isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > page_count
        for page in pages
    ):
        raise CatalogError("evidence-page 必须是 PDF 范围内的正整数")

    book = dict(previous)
    book.update(
        {
            "title": title.strip(),
            "publisher": publisher.strip(),
            "curriculumStandard": curriculum_standard,
            "approvalYear": approval_year,
            "editionLabel": edition_label.strip(),
            "editionNotes": edition_notes.strip(),
            "publicationDate": publication_date,
            "editionNumber": edition_number,
            "printingDate": printing_date,
            "printingNumber": printing_number,
            "redistributionPermissionVerified": (
                public_redistribution_confirmed is True
                and bool(redistribution_evidence.strip())
            ),
            "redistributionEvidence": redistribution_evidence.strip(),
            "evidencePdfPages": pages,
        }
    )
    validate_source_path(book, source_path)
    prepared = prepare_pdf(root, source_path)
    if prepared["sha256"] != pdf_sha:
        raise CatalogError("PDF 在整理过程中发生变化")
    book.update(prepared)

    old_cover_path = previous["coverPath"]
    other_cover_paths = {
        item.get("coverPath") for item in remaining["books"]
    }
    removed_cover = None
    if old_cover_path != prepared["coverPath"] and old_cover_path not in other_cover_paths:
        try:
            old_cover, _ = safe_file(root, old_cover_path)
        except CatalogError:
            old_cover = None
        if old_cover is not None:
            old_cover.unlink()
            removed_cover = old_cover_path

    assets = [source_path, prepared["coverPath"]]
    if removed_cover:
        assets.append(removed_cover)
    assets_committed = _commit_assets(root, assets, edition_id, action="Update")
    book["sourcePath"] = source_path
    pinned = pin_book(root, repository, book)
    candidate = json.loads(json.dumps(catalog))
    candidate["books"][book_index] = pinned
    candidate_for_validation = json.loads(json.dumps(candidate))
    candidate_for_validation["books"] = [
        item
        for item in candidate_for_validation["books"]
        if item.get("sourcePath") not in pending_paths
    ]
    warnings = validate_catalog_data(root, candidate_for_validation)
    write_catalog(root, candidate)
    return pinned, assets_committed, warnings


def delete_books(root: Path, *, source_paths: list[str]) -> list[dict]:
    """Remove catalog entries and any corresponding assets still in the worktree."""
    if not source_paths or len(source_paths) != len(set(source_paths)):
        raise CatalogError("删除路径列表不能为空且不能重复")
    if _has_staged_changes(root):
        raise CatalogError("Git 暂存区已有其他改动；请先提交或取消暂存后重试")
    if not git(root, "config", "user.name") or not git(root, "config", "user.email"):
        raise CatalogError("请先配置本地 Git user.name 与 user.email")

    catalog = load_catalog(root)
    books_by_path = {}
    for source_path in source_paths:
        matches = [book for book in catalog["books"] if book.get("sourcePath") == source_path]
        if len(matches) != 1:
            raise CatalogError(f"必须通过 sourcePath 唯一找到一条记录：{source_path}")
        book = matches[0]
        validate_source_path(book, source_path)
        if book.get("coverPath") != f"covers/{book.get('sha256')}.webp":
            raise CatalogError(f"目录封面路径与 PDF SHA 不匹配，拒绝删除：{source_path}")
        books_by_path[source_path] = book

    candidate = json.loads(json.dumps(catalog))
    candidate["books"] = [
        item
        for item in candidate["books"]
        if item.get("sourcePath") not in books_by_path
    ]
    warnings = validate_catalog_data(root, candidate)
    remaining_cover_paths = {item.get("coverPath") for item in candidate["books"]}
    paths_to_remove = []
    deleted = []
    for source_path, book in books_by_path.items():
        normalized_path = PurePosixPath(source_path).as_posix()
        pdf_file = root / normalized_path
        if pdf_file.is_file():
            safe_file(root, normalized_path)
            paths_to_remove.append(normalized_path)

        cover_path = book["coverPath"]
        deleted_cover = None
        cover_file = root / cover_path
        if cover_path not in remaining_cover_paths and cover_file.is_file():
            _, normalized_cover = safe_file(root, cover_path)
            paths_to_remove.append(normalized_cover)
            deleted_cover = normalized_cover
        deleted.append(
            {"editionId": book["editionId"], "sourcePath": normalized_path, "coverPath": deleted_cover}
        )

    if paths_to_remove:
        git(root, "rm", "--", *sorted(set(paths_to_remove)))
        git(root, "commit", "-m", f"Remove {len(books_by_path)} textbook(s)")
    write_catalog(root, candidate)
    for item in deleted:
        item["warnings"] = warnings
    return deleted


def delete_book(root: Path, *, source_path: str) -> dict:
    """Remove one current catalog entry and any corresponding current assets."""
    return delete_books(root, source_paths=[source_path])[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="教材书库 PDF 准备与目录校验")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="校验目录、Git 来源与所有文件")
    check.set_defaults(command="check")
    prepare = commands.add_parser("prepare", help="检查 PDF 并生成首页 WebP 封面")
    prepare.add_argument("--pdf", required=True, help="相对书库根目录的 PDF 路径")
    pin = commands.add_parser("pin", help="将目录记录绑定到已提交的 PDF/封面 commit")
    pin.set_defaults(command="pin")
    add = commands.add_parser(
        "add",
        help="整理 PDF、提交源文件/封面并创建可下载书目",
        description=(
            "一个命令归一 PDF 路径、生成封面、提交源资源并写入 catalog.json。"
            "需要准确的书目信息；公开再分发信息可选。"
        ),
    )
    add.add_argument("--pdf", required=True, help="当前 PDF 在书库中的相对路径")
    add.add_argument("--subject", required=True, help="学科 slug，例如 math")
    add.add_argument("--grade", required=True, type=int, help="年级，1 到 12")
    add.add_argument(
        "--semester", required=True, choices=("upper", "lower"), help="目标学期目录"
    )
    add.add_argument("--edition-id", required=True, help="稳定的小写 ASCII 版次 ID")
    add.add_argument("--title", required=True, help="书目标题")
    add.add_argument("--publisher", required=True, help="出版社")
    add.add_argument("--edition-label", required=True, help="可见版次标签")
    add.add_argument("--edition-notes", required=True, help="版次事实或补充说明")
    add.add_argument("--curriculum-standard", help="课程标准；未知时留空")
    add.add_argument("--approval-year", type=int, help="审定年份；未知时留空")
    add.add_argument("--publication-date", help="出版日期；未知时留空")
    add.add_argument("--edition-number", type=int, help="版次序号；未知时留空")
    add.add_argument("--printing-date", help="印刷日期；未知时留空")
    add.add_argument("--printing-number", type=int, help="印次；未知时留空")
    add.add_argument(
        "--evidence-page",
        action="append",
        type=int,
        dest="evidence_pages",
        help="版次证据 PDF 页码，可重复；默认第 1 页",
    )
    add.add_argument(
        "--redistribution-evidence",
        default="",
        help="可选的权利方授权依据链接或可追溯记录编号",
    )
    add.add_argument(
        "--confirm-public-redistribution",
        action="store_true",
        help="可选：记录你已核实该文件允许公开再分发",
    )
    add.add_argument(
        "--commit-assets",
        action="store_true",
        required=True,
        help="允许此命令提交它整理的 PDF 与封面 Git 资源",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(json.dumps(prepare_pdf(ROOT, args.pdf), ensure_ascii=False, indent=2))
        elif args.command == "pin":
            pin_catalog(ROOT)
            print("已更新 catalog.json 的 commit、blob、SHA、大小和页数。")
        elif args.command == "add":
            book, assets_committed, warnings = add_book(
                ROOT,
                pdf_path=args.pdf,
                subject=args.subject,
                grade=args.grade,
                semester=args.semester,
                edition_id=args.edition_id,
                title=args.title,
                publisher=args.publisher,
                edition_label=args.edition_label,
                edition_notes=args.edition_notes,
                curriculum_standard=args.curriculum_standard,
                approval_year=args.approval_year,
                publication_date=args.publication_date,
                edition_number=args.edition_number,
                printing_date=args.printing_date,
                printing_number=args.printing_number,
                evidence_pdf_pages=args.evidence_pages,
                redistribution_evidence=args.redistribution_evidence,
                public_redistribution_confirmed=args.confirm_public_redistribution,
            )
            for warning in warnings:
                print(f"警告：{warning}", file=sys.stderr)
            print(f"已登记可下载书目：{book['editionId']}")
            print("catalog.json 已更新但未提交或推送；请检查 diff 后自行同步仓库。")
            if assets_committed:
                print("PDF 与封面已作为独立 Git source commit 提交。")
            else:
                print("PDF 与封面已在当前 Git source commit 中，无需新建资源提交。")
        else:
            warnings = validate_catalog(ROOT)
            for warning in warnings:
                print(f"警告：{warning}")
            print(f"教材目录校验通过：{len(load_catalog(ROOT)['books'])} 条")
        return 0
    except (CatalogError, OSError, ValueError) as error:
        print(f"教材目录校验失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
