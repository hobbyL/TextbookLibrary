#!/usr/bin/env python3
"""Process one textbook intake submitted through GitHub Actions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import PurePosixPath

import validate_catalog as catalog_tool

# Keep these labels and paths aligned with process-textbook.yml.
PDF_CHOICE_PATHS = {
    "数学二年级上册（2024版）": (
        "books/math/grade-02/upper/数学 人教版 二年级 上册 2024版.pdf"
    ),
    "数学二年级下册（2022版）": (
        "books/math/grade-02/lower/数学 人教版 二年级 下册 2022版.pdf"
    ),
    "语文二年级上册（2024版）": (
        "books/chinese/grade-02/upper/语文 统编版 二年级 上册 2024版.pdf"
    ),
    "语文二年级下册（2017版）": (
        "books/chinese/grade-02/lower/语文 统编版 二年级 下册 2017版.pdf"
    ),
    "英语二年级上册（2013版）": (
        "books/english/grade-02/upper/英语 人教版 二年级 上册 2013版.pdf"
    ),
    "英语二年级下册（2013版）": (
        "books/english/grade-02/lower/英语 人教版 二年级 下册 2013版.pdf"
    ),
}


def _optional_string(inputs: dict, name: str) -> str | None:
    value = str(inputs.get(name, "")).strip()
    return value or None


def _optional_int(inputs: dict, name: str) -> int | None:
    value = str(inputs.get(name, "")).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise catalog_tool.CatalogError(f"{name} 必须是整数") from error


def _evidence_pages(inputs: dict, detected_pages: list[int]) -> list[int]:
    raw = str(inputs.get("evidence_pdf_pages", "")).strip()
    if not raw:
        return detected_pages or [1]
    try:
        pages = [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as error:
        raise catalog_tool.CatalogError("evidence_pdf_pages 必须是逗号分隔的页码") from error
    if not pages:
        raise catalog_tool.CatalogError("至少需要一个版次证据页码")
    return pages


def _read_first_pages(root, pdf_path: str, limit: int = 8) -> list[tuple[int, str]]:
    pdf, _ = catalog_tool.safe_file(root, pdf_path)
    try:
        with catalog_tool.pdfium.PdfDocument(str(pdf)) as document:
            pages = []
            for index in range(min(limit, len(document))):
                page = document[index]
                try:
                    text_page = page.get_textpage()
                    try:
                        pages.append((index + 1, text_page.get_text_range()))
                    finally:
                        text_page.close()
                finally:
                    page.close()
    except Exception as error:
        raise catalog_tool.CatalogError(
            f"无法读取 PDF 书名页文字 {pdf_path}: {error}"
        ) from error
    return pages


def _infer_book_metadata(
    pages: list[tuple[int, str]], grade: int, semester: str
) -> tuple[str | None, str | None, list[int]]:
    grade_words = {
        1: "一",
        2: "二",
        3: "三",
        4: "四",
        5: "五",
        6: "六",
        7: "七",
        8: "八",
        9: "九",
        10: "十",
        11: "十一",
        12: "十二",
    }
    semester_label = "上册" if semester == "upper" else "下册"
    grade_label = f"{grade_words.get(grade, '')}年级"
    title_candidates = []
    publisher = None
    publisher_page = None

    for page_number, text in pages:
        for raw_line in text.splitlines():
            line = " ".join(raw_line.split())
            if (
                "义务教育教科书" in line
                and grade_label != "年级"
                and grade_label in line
                and semester_label in line
            ):
                title_candidates.append(
                    (line.startswith("义务教育教科书"), len(line), page_number, line)
                )
            if publisher is None:
                match = re.search(r"([\u3400-\u9fff]{2,12}出版社)", line)
                if match:
                    publisher = match.group(1)
                    publisher_page = page_number
        if publisher is None and re.search(r"pep\.com\.cn", text, re.IGNORECASE):
            publisher = "人民教育出版社"
            publisher_page = page_number

    if title_candidates:
        _, _, title_page, title = max(title_candidates)
        title = title[: title.rfind(semester_label) + len(semester_label)].strip()
    else:
        title = None
        title_page = None

    evidence_pages = sorted(
        {page for page in (title_page, publisher_page) if page is not None}
    )
    return title, publisher, evidence_pages


def _resolve_pdf_paths(root, inputs: dict, catalog: dict) -> list[str]:
    catalogued = {
        book.get("sourcePath")
        for book in catalog.get("books", [])
        if isinstance(book.get("sourcePath"), str)
    }
    requested = _optional_string(inputs, "pdf_path")
    if requested:
        _, relative = catalog_tool.safe_file(root, requested)
        if relative in catalogued:
            raise catalog_tool.CatalogError(f"该 PDF 已登记：{relative}")
        return [relative]
    choice = _optional_string(inputs, "pdf_choice")
    if choice and choice != "auto":
        selected = PDF_CHOICE_PATHS.get(choice)
        if not selected:
            raise catalog_tool.CatalogError(f"下拉菜单中的 PDF 选项无效：{choice}")
        _, relative = catalog_tool.safe_file(root, selected)
        if relative in catalogued:
            raise catalog_tool.CatalogError(f"该 PDF 已登记：{relative}")
        return [relative]

    commits = catalog_tool.git(
        root, "log", "--format=%H", "--diff-filter=AM", "--", "books"
    ).splitlines()
    latest_pdf_changes = None
    for commit in commits:
        changed_paths = catalog_tool.git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "-z",
            "-r",
            commit,
            "--",
            "books",
        ).split("\0")
        pdf_changes = [path for path in changed_paths if path.lower().endswith(".pdf")]
        if pdf_changes:
            latest_pdf_changes = pdf_changes
            break

    if latest_pdf_changes is not None:
        candidates = sorted(
            path
            for path in latest_pdf_changes
            if path not in catalogued and (root / path).is_file()
        )
        if candidates:
            return candidates
        raise catalog_tool.CatalogError(
            "最近一次 PDF 变更提交中的文件均已登记或不存在；请选择其他未登记 PDF"
        )

    candidates = sorted(
        path.relative_to(root).as_posix()
        for path in (root / "books").rglob("*.pdf")
        if path.is_file() and path.relative_to(root).as_posix() not in catalogued
    )
    if len(candidates) == 1:
        return candidates
    if not candidates:
        raise catalog_tool.CatalogError(
            "没有找到尚未登记的 PDF；请先将 PDF 上传到 books/，或填写 pdf_path"
        )
    raise catalog_tool.CatalogError(
        "无法唯一确定要处理的 PDF，请填写 pdf_path：" + ", ".join(candidates)
    )


def _path_metadata(pdf_path: str) -> tuple[str, int, str]:
    parts = PurePosixPath(pdf_path).parts
    if len(parts) != 5 or parts[0] != "books" or not parts[4].endswith(".pdf"):
        raise catalog_tool.CatalogError(
            "PDF 路径必须符合 books/<subject>/grade-<n>/<upper|lower>/<file>.pdf"
        )
    grade_match = re.fullmatch(r"grade-(\d{1,2})", parts[2])
    if not grade_match or parts[3] not in {"upper", "lower"}:
        raise catalog_tool.CatalogError(
            "PDF 路径必须包含 grade-<n> 和 upper/lower 学期目录"
        )
    subject = parts[1]
    grade = int(grade_match.group(1))
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", subject):
        raise catalog_tool.CatalogError(f"subject 目录名无效：{subject}")
    if not 1 <= grade <= 12:
        raise catalog_tool.CatalogError(f"grade 目录名无效：{parts[2]}")
    return subject, grade, parts[3]


def _generated_edition_id(pdf_path: str, subject: str, grade: int, semester: str) -> str:
    stem = PurePosixPath(pdf_path).stem.lower()
    filename_slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:40]
    path_hash = hashlib.sha256(pdf_path.encode("utf-8")).hexdigest()[:10]
    suffix = filename_slug or "edition"
    return f"{subject}-grade-{grade:02d}-{semester}-{suffix}-{path_hash}"


def _edition_label_from_filename(pdf_path: str) -> str | None:
    stem = PurePosixPath(pdf_path).stem
    for pattern in (
        r"(?<!\d)(20\d{2})\s*年版",
        r"(?<!\d)(20\d{2})\s*版",
        r"(第[一二三四五六七八九十0-9]+版)",
    ):
        match = re.search(pattern, stem)
        if match:
            return match.group(0).replace(" ", "")
    return None


def _generated_edition_notes(
    title: str,
    publisher: str,
    edition_label: str,
    evidence_pages: list[int],
    edition_label_source: str,
) -> str:
    pages = "、".join(str(page) for page in evidence_pages)
    pages = pages or "未自动识别到文字证据页"
    return (
        f"书名“{title}”；出版社“{publisher}”；版次标签“{edition_label}”"
        f"（{edition_label_source}）；证据页：{pages}。请核对原书版权页。"
    )


def process(
    inputs: dict,
    *,
    root=catalog_tool.ROOT,
    github_ref_name: str | None = None,
    github_repository: str | None = None,
) -> None:
    branch = github_ref_name or os.environ.get("GITHUB_REF_NAME")
    if branch != "main":
        raise catalog_tool.CatalogError("请在 main 分支运行此工作流")
    catalog = catalog_tool.load_catalog(root)
    repository = catalog["repository"]
    action_repository = github_repository or os.environ.get("GITHUB_REPOSITORY")
    if action_repository != repository:
        raise catalog_tool.CatalogError("工作流仓库与 catalog.repository 不一致")

    pdf_paths = _resolve_pdf_paths(root, inputs, catalog)
    if len(pdf_paths) > 1:
        per_book_fields = (
            "edition_id",
            "title",
            "publisher",
            "edition_label",
            "edition_notes",
            "curriculum_standard",
            "approval_year",
            "publication_date",
            "edition_number",
            "printing_date",
            "printing_number",
            "evidence_pdf_pages",
        )
        provided = [name for name in per_book_fields if _optional_string(inputs, name)]
        if provided:
            raise catalog_tool.CatalogError(
                "批量处理多本 PDF 时请清空单本元数据覆盖项：" + ", ".join(provided)
            )

    confirmed = inputs.get("confirm_public_redistribution")
    confirmed = confirmed is True or str(confirmed).lower() == "true"
    redistribution_evidence = str(inputs.get("redistribution_evidence", "")).strip()
    if not confirmed:
        raise catalog_tool.CatalogError("必须明确确认所有选中的 PDF 均可公开再分发")
    if not redistribution_evidence:
        raise catalog_tool.CatalogError(
            "必须提供覆盖所有选中 PDF 的可追溯公开再分发授权依据"
        )

    prepared_books = []
    for pdf_path in pdf_paths:
        subject, grade, semester = _path_metadata(pdf_path)
        supplied_edition_label = _optional_string(inputs, "edition_label")
        filename_edition_label = _edition_label_from_filename(pdf_path)
        edition_label = supplied_edition_label or filename_edition_label
        if not edition_label:
            raise catalog_tool.CatalogError(
                f"无法从 PDF 文件名识别版次标签，请填写 edition_label：{pdf_path}"
            )
        inferred_title, inferred_publisher, detected_pages = _infer_book_metadata(
            _read_first_pages(root, pdf_path), grade, semester
        )
        title = _optional_string(inputs, "title") or inferred_title
        if not title:
            raise catalog_tool.CatalogError(
                f"无法从 PDF 前 8 页识别书名，请填写 title：{pdf_path}"
            )
        publisher = _optional_string(inputs, "publisher") or inferred_publisher
        if not publisher:
            raise catalog_tool.CatalogError(
                f"无法从 PDF 前 8 页识别出版社，请填写 publisher：{pdf_path}"
            )
        evidence_pages = _evidence_pages(inputs, detected_pages)
        edition_notes = _optional_string(inputs, "edition_notes") or _generated_edition_notes(
            title,
            publisher,
            edition_label,
            evidence_pages,
            "手动填写" if supplied_edition_label else "文件名识别",
        )
        prepared_books.append(
            {
                "pdf_path": pdf_path,
                "subject": subject,
                "grade": grade,
                "semester": semester,
                "edition_id": _optional_string(inputs, "edition_id")
                or _generated_edition_id(pdf_path, subject, grade, semester),
                "title": title,
                "publisher": publisher,
                "edition_label": edition_label,
                "edition_notes": edition_notes,
                "evidence_pages": evidence_pages,
            }
        )

    for details in prepared_books:
        print(f"开始登记：{details['pdf_path']}")
        print(f"书名：{details['title']}")
        print(f"出版社：{details['publisher']}")
        print(f"版次标签：{details['edition_label']}")
        print(f"证据页：{', '.join(str(page) for page in details['evidence_pages'])}")
        book, assets_committed, warnings = catalog_tool.add_book(
            root,
            pdf_path=details["pdf_path"],
            subject=details["subject"],
            grade=details["grade"],
            semester=details["semester"],
            edition_id=details["edition_id"],
            title=details["title"],
            publisher=details["publisher"],
            edition_label=details["edition_label"],
            edition_notes=details["edition_notes"],
            redistribution_evidence=redistribution_evidence,
            public_redistribution_confirmed=confirmed,
            curriculum_standard=_optional_string(inputs, "curriculum_standard"),
            approval_year=_optional_int(inputs, "approval_year"),
            publication_date=_optional_string(inputs, "publication_date"),
            edition_number=_optional_int(inputs, "edition_number"),
            printing_date=_optional_string(inputs, "printing_date"),
            printing_number=_optional_int(inputs, "printing_number"),
            evidence_pdf_pages=details["evidence_pages"],
        )
        for warning in warnings:
            print(f"警告：{warning}", file=sys.stderr)
        print(f"已登记可下载书目：{book['editionId']}")
        print(f"PDF：{book['sourcePath']}")
        print(f"SHA-256：{book['sha256']}")
        print(f"source commit：{book['sourceCommit']}")
        print("封面资源已提交。" if assets_committed else "封面已在当前 source commit 中。")


def main() -> int:
    try:
        raw = os.environ.get("TEXTBOOK_ACTION_INPUTS", "")
        inputs = json.loads(raw)
        if not isinstance(inputs, dict):
            raise catalog_tool.CatalogError("GitHub Actions 表单输入格式无效")
        process(inputs)
        return 0
    except (catalog_tool.CatalogError, KeyError, OSError, TypeError, ValueError) as error:
        print(f"教材目录自动登记失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
