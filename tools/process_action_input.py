#!/usr/bin/env python3
"""Process one textbook intake submitted through GitHub Actions."""

from __future__ import annotations

import json
import os
import sys

import validate_catalog as catalog_tool


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


def _evidence_pages(inputs: dict) -> list[int]:
    raw = str(inputs.get("evidence_pdf_pages", "1")).strip()
    try:
        pages = [int(value.strip()) for value in raw.split(",") if value.strip()]
    except ValueError as error:
        raise catalog_tool.CatalogError("evidence_pdf_pages 必须是逗号分隔的页码") from error
    if not pages:
        raise catalog_tool.CatalogError("至少需要一个版次证据页码")
    return pages


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
    repository = catalog_tool.load_catalog(root)["repository"]
    action_repository = github_repository or os.environ.get("GITHUB_REPOSITORY")
    if action_repository != repository:
        raise catalog_tool.CatalogError("工作流仓库与 catalog.repository 不一致")

    confirmed = inputs.get("confirm_public_redistribution")
    confirmed = confirmed is True or str(confirmed).lower() == "true"
    book, assets_committed, warnings = catalog_tool.add_book(
        root,
        pdf_path=str(inputs.get("pdf_path", "")),
        subject=str(inputs.get("subject", "")),
        grade=int(inputs.get("grade", "0")),
        semester=str(inputs.get("semester", "")),
        edition_id=str(inputs.get("edition_id", "")),
        title=str(inputs.get("title", "")),
        publisher=str(inputs.get("publisher", "")),
        edition_label=str(inputs.get("edition_label", "")),
        edition_notes=str(inputs.get("edition_notes", "")),
        redistribution_evidence=str(inputs.get("redistribution_evidence", "")),
        public_redistribution_confirmed=confirmed,
        curriculum_standard=_optional_string(inputs, "curriculum_standard"),
        approval_year=_optional_int(inputs, "approval_year"),
        publication_date=_optional_string(inputs, "publication_date"),
        edition_number=_optional_int(inputs, "edition_number"),
        printing_date=_optional_string(inputs, "printing_date"),
        printing_number=_optional_int(inputs, "printing_number"),
        evidence_pdf_pages=_evidence_pages(inputs),
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
