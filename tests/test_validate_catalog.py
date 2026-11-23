from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import process_action_input as action_input
import validate_catalog as validator


def one_page_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Resources << >> /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(output)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


class ValidateCatalogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copyfile(
            Path(validator.__file__).resolve().parents[1] / "catalog.schema.json",
            self.root / "catalog.schema.json",
        )
        (self.root / "books/math/grade-01/upper").mkdir(parents=True)
        pdf = self.root / "books/math/grade-01/upper/pep-math-1a-2024.pdf"
        pdf.write_bytes(one_page_pdf())
        snippet = validator.prepare_pdf(
            self.root,
            "books/math/grade-01/upper/pep-math-1a-2024.pdf",
        )
        self.cover_path = self.root / snippet["coverPath"]
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "textbook-test@example.invalid")
        git(self.root, "config", "user.name", "Textbook Test")
        git(self.root, "add", "books", "covers")
        git(self.root, "commit", "-qm", "Add textbook fixture")
        self.catalog = {
            "schemaVersion": 1,
            "repository": "hobbyL/TextbookLibrary",
            "books": [
                {
                    "subject": "math",
                    "grade": 1,
                    "semester": "上",
                    "title": "人教版数学",
                    "publisher": "人民教育出版社",
                    "editionId": "pep-math-1a-2024",
                    "curriculumStandard": None,
                    "approvalYear": 2024,
                    "editionLabel": "教育部审定2024",
                    "editionNotes": "测试教材记录。",
                    "publicationDate": None,
                    "editionNumber": None,
                    "printingDate": None,
                    "printingNumber": None,
                    "sourcePath": "books/math/grade-01/upper/pep-math-1a-2024.pdf",
                    "coverPath": snippet["coverPath"],
                    "downloadable": True,
                    "redistributionPermissionVerified": True,
                    "redistributionEvidence": "测试许可记录",
                    **snippet,
                }
            ],
        }
        (self.root / "catalog.json").write_text(
            json.dumps(self.catalog, ensure_ascii=False), encoding="utf-8"
        )
        validator.pin_catalog(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_library_fixture(self, semester: str = "upper") -> tuple[Path, str]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        shutil.copyfile(
            Path(validator.__file__).resolve().parents[1] / "catalog.schema.json",
            root / "catalog.schema.json",
        )
        (root / "catalog.json").write_text(
            json.dumps(
                {"schemaVersion": 1, "repository": "hobbyL/TextbookLibrary", "books": []},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        source = (
            f"books/math/grade-02/{semester}/"
            "数学 人教版 二年级 上册 2024版.pdf"
        )
        source_path = root / source
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(one_page_pdf())
        git(root, "init", "-q")
        git(root, "config", "user.email", "textbook-test@example.invalid")
        git(root, "config", "user.name", "Textbook Test")
        git(root, "add", "catalog.json", "catalog.schema.json")
        git(root, "commit", "-qm", "Initialize test library")
        return root, source

    def add_book(self, root: Path, source: str) -> tuple[dict, bool, list[str]]:
        return validator.add_book(
            root,
            pdf_path=source,
            subject="math",
            grade=2,
            semester="upper",
            edition_id="pep-math-2a-2024",
            title="人教版数学二年级上册",
            publisher="人民教育出版社",
            edition_label="2024版",
            edition_notes="依据版权页登记版次。",
            approval_year=2024,
            redistribution_evidence="https://example.invalid/permission/123",
            public_redistribution_confirmed=True,
        )

    def test_valid_committed_pdf_and_cover_pass(self) -> None:
        warnings = validator.validate_catalog(self.root)
        self.assertEqual(warnings, [])

    def test_downloadable_book_allows_unverified_redistribution(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["redistributionPermissionVerified"] = False
        catalog["books"][0]["redistributionEvidence"] = ""
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        self.assertTrue(catalog["books"][0]["downloadable"])
        self.assertEqual(validator.validate_catalog(self.root), [])

    def test_path_must_match_book_identity(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["sourcePath"] = "../outside.pdf"
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "sourcePath"):
            validator.validate_catalog(self.root)

    def test_pdf_hash_mismatch_fails(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["sha256"] = "0" * 64
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "coverPath"):
            validator.validate_catalog(self.root)

    def test_duplicate_edition_id_fails(self) -> None:
        catalog = validator.load_catalog(self.root)
        duplicate = dict(catalog["books"][0])
        duplicate["sha256"] = "0" * 64
        catalog["books"].append(duplicate)
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "版次 ID 重复"):
            validator.validate_catalog(self.root)

    def test_duplicate_pdf_sha_fails(self) -> None:
        catalog = validator.load_catalog(self.root)
        duplicate = dict(catalog["books"][0])
        duplicate["editionId"] = "pep-math-1a-2025"
        catalog["books"].append(duplicate)
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "PDF SHA-256 重复"):
            validator.validate_catalog(self.root)

    def test_pdf_length_and_page_count_mismatches_fail(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["bytes"] += 1
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "PDF 字节数不匹配"):
            validator.validate_catalog(self.root)

        catalog["books"][0]["bytes"] -= 1
        catalog["books"][0]["pageCount"] += 1
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "PDF 页数不匹配"):
            validator.validate_catalog(self.root)

    def test_git_commit_and_blob_mismatches_fail(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["sourceCommit"] = "0" * 40
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "git rev-parse"):
            validator.validate_catalog(self.root)

        catalog["books"][0]["sourceCommit"] = git(self.root, "rev-parse", "HEAD")
        catalog["books"][0]["gitBlobSha"] = "0" * 40
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "Git blob SHA 不匹配"):
            validator.validate_catalog(self.root)

    def test_cover_hash_mismatch_fails(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["coverSha256"] = "0" * 64
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "封面 SHA-256 不匹配"):
            validator.validate_catalog(self.root)

    def test_corrupt_pdf_cannot_be_prepared(self) -> None:
        path = self.root / "books/math/grade-01/upper/corrupt.pdf"
        path.write_bytes(b"not a PDF")
        with self.assertRaisesRegex(validator.CatalogError, "PDFium 无法打开"):
            validator.prepare_pdf(self.root, "books/math/grade-01/upper/corrupt.pdf")

    def test_catalog_schema_and_corrupt_cover_fail(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["grade"] = 0
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "catalog.json schema"):
            validator.validate_catalog(self.root)

        catalog["books"][0]["grade"] = 1
        self.cover_path.write_bytes(b"not a WebP")
        catalog["books"][0]["coverSha256"] = validator.sha256_file(self.cover_path)
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "封面无法解码"):
            validator.validate_catalog(self.root)

    def test_add_preserves_original_filename_and_writes_valid_row(self) -> None:
        root, source = self.add_library_fixture()

        book, assets_committed, warnings = self.add_book(root, source)

        self.assertTrue(assets_committed)
        self.assertEqual(warnings, [])
        self.assertTrue((root / source).is_file())
        self.assertTrue((root / book["coverPath"]).is_file())
        self.assertEqual(book["sourcePath"], source)
        self.assertEqual(book["editionId"], "pep-math-2a-2024")
        self.assertTrue(book["redistributionPermissionVerified"])
        self.assertEqual(
            book["redistributionEvidence"], "https://example.invalid/permission/123"
        )
        self.assertEqual(book["sourceCommit"], git(root, "rev-parse", "HEAD"))
        self.assertEqual(len(validator.load_catalog(root)["books"]), 1)
        self.assertEqual(validator.validate_catalog(root), [])

    def test_add_allows_missing_or_incomplete_rights_metadata(self) -> None:
        cases = (
            {},
            {"public_redistribution_confirmed": True},
            {"redistribution_evidence": "测试许可记录"},
        )
        for rights_metadata in cases:
            with self.subTest(rights_metadata=rights_metadata):
                root, source = self.add_library_fixture()
                book, _, warnings = validator.add_book(
                    root,
                    pdf_path=source,
                    subject="math",
                    grade=2,
                    semester="upper",
                    edition_id="pep-math-2a-2024",
                    title="人教版数学二年级上册",
                    publisher="人民教育出版社",
                    edition_label="2024版",
                    edition_notes="依据版权页登记版次。",
                    **rights_metadata,
                )

                self.assertTrue(book["downloadable"])
                self.assertFalse(book["redistributionPermissionVerified"])
                self.assertEqual(
                    book["redistributionEvidence"],
                    rights_metadata.get("redistribution_evidence", ""),
                )
                self.assertEqual(warnings, [])
                self.assertEqual(validator.validate_catalog(root), [])

    def test_add_cli_does_not_require_redistribution_arguments(self) -> None:
        root, source = self.add_library_fixture()
        with patch.object(validator, "ROOT", root):
            result = validator.main(
                [
                    "add", "--pdf", source,
                    "--subject", "math", "--grade", "2", "--semester", "upper",
                    "--edition-id", "pep-math-2a-2024",
                    "--title", "人教版数学二年级上册",
                    "--publisher", "人民教育出版社",
                    "--edition-label", "2024版",
                    "--edition-notes", "依据版权页登记版次。",
                    "--commit-assets",
                ]
            )

        self.assertEqual(result, 0)
        book = validator.load_catalog(root)["books"][0]
        self.assertTrue(book["downloadable"])
        self.assertFalse(book["redistributionPermissionVerified"])
        self.assertEqual(book["redistributionEvidence"], "")
        self.assertEqual(validator.validate_catalog(root), [])

    def test_update_replaces_existing_record_and_removes_old_cover(self) -> None:
        root, source = self.add_library_fixture()
        original = self.add_book(root, source)[0]
        old_cover_path = original["coverPath"]
        old_sha = original["sha256"]
        old_edition_id = original["editionId"]

        (root / source).write_bytes(one_page_pdf() + b"\n% replacement textbook PDF\n")
        git(root, "add", "--", source)
        git(root, "commit", "-qm", "Replace current textbook PDF")

        updated, assets_committed, warnings = validator.update_book(
            root,
            pdf_path=source,
            subject="math",
            grade=2,
            semester="upper",
            edition_id=old_edition_id,
            title="人教版数学二年级上册新版",
            publisher="人民教育出版社",
            edition_label="2025版",
            edition_notes="已更新当前版次记录。",
            public_redistribution_confirmed=False,
        )

        self.assertTrue(assets_committed)
        self.assertEqual(warnings, [])
        self.assertEqual(updated["sourcePath"], source)
        self.assertEqual(updated["editionId"], old_edition_id)
        self.assertNotEqual(updated["sha256"], old_sha)
        self.assertNotEqual(updated["coverPath"], old_cover_path)
        self.assertFalse((root / old_cover_path).exists())
        self.assertEqual(len(validator.load_catalog(root)["books"]), 1)
        self.assertEqual(validator.load_catalog(root)["books"][0]["title"], "人教版数学二年级上册新版")
        self.assertEqual(validator.validate_catalog(root), [])

    def test_delete_removes_current_book_pdf_and_unshared_cover(self) -> None:
        root, source = self.add_library_fixture()
        book = self.add_book(root, source)[0]
        cover_path = book["coverPath"]

        deleted = validator.delete_book(root, source_path=source)

        self.assertEqual(deleted["editionId"], book["editionId"])
        self.assertEqual(deleted["coverPath"], cover_path)
        self.assertFalse((root / source).exists())
        self.assertFalse((root / cover_path).exists())
        self.assertEqual(validator.load_catalog(root)["books"], [])
        self.assertEqual(validator.validate_catalog(root), [])

    def test_add_refuses_staged_changes_before_rehoming(self) -> None:
        root, source = self.add_library_fixture()
        (root / "unrelated.txt").write_text("keep staged", encoding="utf-8")
        git(root, "add", "unrelated.txt")

        with self.assertRaisesRegex(validator.CatalogError, "暂存区已有其他改动"):
            self.add_book(root, source)

        self.assertTrue((root / source).is_file())
        self.assertEqual(validator.load_catalog(root)["books"], [])
        self.assertFalse((root / "covers").exists())

    def test_add_rejects_wrong_semester_directory_without_renaming(self) -> None:
        root, source = self.add_library_fixture(semester="lower")

        with self.assertRaisesRegex(validator.CatalogError, "学期目录"):
            self.add_book(root, source)

        self.assertTrue((root / source).is_file())
        self.assertEqual(validator.load_catalog(root)["books"], [])
        self.assertFalse((root / "covers").exists())

    def test_add_refuses_duplicate_edition_without_changing_catalog(self) -> None:
        root, source = self.add_library_fixture()
        self.add_book(root, source)
        before = (root / "catalog.json").read_bytes()
        with self.assertRaisesRegex(validator.CatalogError, "版次已登记"):
            self.add_book(root, source)

        self.assertEqual((root / "catalog.json").read_bytes(), before)

    def test_action_form_processes_original_filename_and_updates_catalog(self) -> None:
        root, source = self.add_library_fixture()
        inputs = {
            "approval_year": "2024",
            "redistribution_evidence": "https://example.invalid/permission/123",
            "confirm_public_redistribution": True,
        }

        with patch.object(
            action_input,
            "_read_first_pages",
            return_value=[
                (
                    1,
                    "义务教育教科书 数学 二年级 上册\n"
                    "人民教育出版社 课程教材研究所 编著",
                ),
            ],
        ):
            action_input.process(
                inputs,
                root=root,
                github_ref_name="main",
                github_repository="hobbyL/TextbookLibrary",
            )

        catalog = validator.load_catalog(root)
        self.assertEqual(len(catalog["books"]), 1)
        book = catalog["books"][0]
        self.assertEqual(book["sourcePath"], source)
        self.assertRegex(
            book["editionId"], r"^math-grade-02-upper-2024-[a-f0-9]{10}$"
        )
        self.assertEqual(book["subject"], "math")
        self.assertEqual(book["grade"], 2)
        self.assertEqual(book["semester"], "上")
        self.assertEqual(book["title"], "义务教育教科书 数学 二年级 上册")
        self.assertEqual(book["publisher"], "人民教育出版社")
        self.assertEqual(book["editionLabel"], "2024版")
        self.assertIn("证据页：1", book["editionNotes"])
        self.assertEqual(book["evidencePdfPages"], [1])
        self.assertTrue(book["downloadable"])
        self.assertEqual(validator.validate_catalog(root), [])

    def test_action_add_auto_returns_all_unregistered_pdfs(self) -> None:
        root, first_source = self.add_library_fixture()
        second_pdf = root / "books/math/grade-02/lower/another-book.pdf"
        second_pdf.parent.mkdir(parents=True)
        second_pdf.write_bytes(one_page_pdf())

        candidates = action_input._resolve_pdf_paths(
            root, {"pdf_choice": "auto"}, validator.load_catalog(root)
        )

        self.assertEqual(
            candidates,
            sorted(
                [
                    first_source,
                    "books/math/grade-02/lower/another-book.pdf",
                ]
            ),
        )

    def test_action_processes_single_book_without_confirmation(self) -> None:
        root, source = self.add_library_fixture()
        action_input.process(
            {
                "pdf_choice": "auto",
                "confirm_public_redistribution": False,
                "title": "义务教育教科书 数学 二年级 上册",
                "publisher": "人民教育出版社",
            },
            root=root,
            github_ref_name="main",
            github_repository="hobbyL/TextbookLibrary",
        )

        book = validator.load_catalog(root)["books"][0]
        self.assertEqual(book["sourcePath"], source)
        self.assertTrue(book["downloadable"])
        self.assertFalse(book["redistributionPermissionVerified"])
        self.assertEqual(book["redistributionEvidence"], "")
        self.assertEqual(validator.validate_catalog(root), [])

    def test_action_updates_existing_record_in_place(self) -> None:
        root, source = self.add_library_fixture()
        original = self.add_book(root, source)[0]
        old_sha = original["sha256"]
        (root / source).write_bytes(one_page_pdf() + b"\n% updated through action\n")
        git(root, "add", "--", source)
        git(root, "commit", "-qm", "Replace textbook PDF")

        head_before = git(root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(validator.CatalogError, "保留原 editionId"):
            action_input.process(
                {
                    "operation": "update",
                    "pdf_choice": "auto",
                    "edition_id": "replacement-id-is-not-allowed",
                    "title": "新版数学教材",
                    "publisher": "人民教育出版社",
                    "edition_label": "2025版",
                },
                root=root,
                github_ref_name="main",
                github_repository="hobbyL/TextbookLibrary",
            )
        self.assertEqual(git(root, "rev-parse", "HEAD"), head_before)

        action_input.process(
            {
                "operation": "update",
                "pdf_choice": "auto",
                "title": "人教版数学二年级上册新版",
                "publisher": "人民教育出版社",
                "edition_label": "2025版",
                "confirm_public_redistribution": False,
            },
            root=root,
            github_ref_name="main",
            github_repository="hobbyL/TextbookLibrary",
        )

        catalog = validator.load_catalog(root)
        self.assertEqual(len(catalog["books"]), 1)
        updated = catalog["books"][0]
        self.assertEqual(updated["sourcePath"], source)
        self.assertEqual(updated["editionId"], original["editionId"])
        self.assertNotEqual(updated["sha256"], old_sha)
        self.assertEqual(updated["title"], "人教版数学二年级上册新版")
        self.assertEqual(updated["editionLabel"], "2025版")
        self.assertEqual(validator.validate_catalog(root), [])

    def test_update_auto_finds_books_changed_in_separate_commits(self) -> None:
        root, first_source = self.add_library_fixture()
        first = self.add_book(root, first_source)[0]

        second_source = (
            "books/chinese/grade-02/lower/语文 统编版 二年级 下册 2017版.pdf"
        )
        second_pdf = root / second_source
        second_pdf.parent.mkdir(parents=True)
        second_pdf.write_bytes(one_page_pdf() + b"\n% original second PDF\n")
        second, _, _ = validator.add_book(
            root,
            pdf_path=second_source,
            subject="chinese",
            grade=2,
            semester="lower",
            edition_id="chinese-grade-02-lower-2017-test",
            title="义务教育教科书 语文 二年级 下册",
            publisher="人民教育出版社",
            edition_label="2017版",
            edition_notes="测试语文教材记录。",
        )

        (root / first_source).write_bytes(one_page_pdf() + b"\n% updated math PDF\n")
        git(root, "add", "--", first_source)
        git(root, "commit", "-qm", "Update math PDF")
        (root / second_source).write_bytes(one_page_pdf() + b"\n% updated Chinese PDF\n")
        git(root, "add", "--", second_source)
        git(root, "commit", "-qm", "Update Chinese PDF")

        def page_text(_root: Path, pdf_path: str):
            if "/chinese/" in pdf_path:
                return [(1, "义务教育教科书 语文 二年级 下册\n人民教育出版社")]
            return [(1, "义务教育教科书 数学 二年级 上册\n人民教育出版社")]

        with patch.object(action_input, "_read_first_pages", side_effect=page_text):
            action_input.process(
                {"operation": "update", "pdf_choice": "auto"},
                root=root,
                github_ref_name="main",
                github_repository="hobbyL/TextbookLibrary",
            )

        catalog = validator.load_catalog(root)
        self.assertCountEqual(
            [book["sourcePath"] for book in catalog["books"]],
            [first_source, second_source],
        )
        updated_by_path = {book["sourcePath"]: book for book in catalog["books"]}
        self.assertEqual(updated_by_path[first_source]["editionId"], first["editionId"])
        self.assertEqual(updated_by_path[second_source]["editionId"], second["editionId"])
        self.assertNotEqual(updated_by_path[first_source]["sha256"], first["sha256"])
        self.assertNotEqual(updated_by_path[second_source]["sha256"], second["sha256"])
        self.assertEqual(validator.validate_catalog(root), [])

    def test_action_delete_requires_confirmation_and_removes_selected_book(self) -> None:
        root, source = self.add_library_fixture()
        book = self.add_book(root, source)[0]
        head_before = git(root, "rev-parse", "HEAD")
        base_inputs = {"operation": "delete", "pdf_path": source}

        with self.assertRaisesRegex(validator.CatalogError, "confirm_delete"):
            action_input.process(
                base_inputs,
                root=root,
                github_ref_name="main",
                github_repository="hobbyL/TextbookLibrary",
            )

        self.assertEqual(git(root, "rev-parse", "HEAD"), head_before)
        self.assertTrue((root / source).is_file())
        self.assertEqual(len(validator.load_catalog(root)["books"]), 1)

        action_input.process(
            {**base_inputs, "confirm_delete": True},
            root=root,
            github_ref_name="main",
            github_repository="hobbyL/TextbookLibrary",
        )

        self.assertFalse((root / source).exists())
        self.assertFalse((root / book["coverPath"]).exists())
        self.assertEqual(validator.load_catalog(root)["books"], [])
        self.assertEqual(validator.validate_catalog(root), [])

    def test_action_add_auto_processes_all_unregistered_pdfs_across_commits(self) -> None:
        root, first_source = self.add_library_fixture()
        git(root, "add", "--", first_source)
        git(root, "commit", "-qm", "Upload math PDF")

        second_source = (
            "books/chinese/grade-02/lower/"
            "语文 统编版 二年级 下册 2017版.pdf"
        )
        second_pdf = root / second_source
        second_pdf.parent.mkdir(parents=True)
        second_pdf.write_bytes(one_page_pdf() + b"\n% second fixture\n")
        git(root, "add", "--", second_source)
        git(root, "commit", "-qm", "Upload Chinese PDF")
        (root / "workflow-update.txt").write_text("code-only commit\n", encoding="utf-8")
        git(root, "add", "workflow-update.txt")
        git(root, "commit", "-qm", "Update workflow")

        def page_text(_root: Path, pdf_path: str):
            if "/chinese/" in pdf_path:
                return [(1, "义务教育教科书 语文 二年级 下册\n人民教育出版社")]
            return [(1, "义务教育教科书 数学 二年级 上册\n人民教育出版社")]

        inputs = {"pdf_choice": "auto", "confirm_public_redistribution": False}
        head_before = git(root, "rev-parse", "HEAD")
        with patch.object(action_input, "_read_first_pages", side_effect=page_text):
            with self.assertRaisesRegex(validator.CatalogError, "单本元数据覆盖项"):
                action_input.process(
                    {**inputs, "title": "不应套用到所有书的标题"},
                    root=root,
                    github_ref_name="main",
                    github_repository="hobbyL/TextbookLibrary",
                )
        self.assertEqual(git(root, "rev-parse", "HEAD"), head_before)

        with patch.object(action_input, "_read_first_pages", side_effect=page_text):
            action_input.process(
                inputs,
                root=root,
                github_ref_name="main",
                github_repository="hobbyL/TextbookLibrary",
            )

        catalog = validator.load_catalog(root)
        self.assertCountEqual(
            [book["sourcePath"] for book in catalog["books"]],
            [first_source, second_source],
        )
        self.assertEqual(len(catalog["books"]), 2)
        for book in catalog["books"]:
            self.assertTrue(book["downloadable"])
            self.assertFalse(book["redistributionPermissionVerified"])
            self.assertEqual(book["redistributionEvidence"], "")
        self.assertEqual(validator.validate_catalog(root), [])

    def test_pdf_choice_resolves_to_its_repository_path(self) -> None:
        root, source = self.add_library_fixture()
        selected = action_input._resolve_pdf_paths(
            root,
            {"pdf_choice": "数学二年级上册（2024版）"},
            validator.load_catalog(root),
        )

        self.assertEqual(selected, [source])

    def test_metadata_inference_prefers_complete_title_page_line(self) -> None:
        title, publisher, pages = action_input._infer_book_metadata(
            [
                (1, "下册二年级义务教育教科书"),
                (
                    3,
                    "义务教育教科书 语文 二年级 下册\n"
                    "http://www.pep.com.cn",
                ),
            ],
            grade=2,
            semester="lower",
        )

        self.assertEqual(title, "义务教育教科书 语文 二年级 下册")
        self.assertEqual(publisher, "人民教育出版社")
        self.assertEqual(pages, [3])

    def test_action_form_rejects_non_main_branch_before_processing(self) -> None:
        root, source = self.add_library_fixture()
        inputs = {
            "pdf_path": source,
            "subject": "math",
            "grade": "2",
            "semester": "upper",
            "edition_id": "pep-math-2a-2024",
            "title": "人教版数学二年级上册",
            "publisher": "人民教育出版社",
            "edition_label": "2024版",
            "edition_notes": "依据版权页登记版次。",
            "redistribution_evidence": "https://example.invalid/permission/123",
            "confirm_public_redistribution": True,
        }

        with self.assertRaisesRegex(validator.CatalogError, "main 分支"):
            action_input.process(
                inputs,
                root=root,
                github_ref_name="review",
                github_repository="hobbyL/TextbookLibrary",
            )

        self.assertTrue((root / source).is_file())
        self.assertEqual(validator.load_catalog(root)["books"], [])

    def test_action_form_rejects_repository_mismatch_before_processing(self) -> None:
        root, source = self.add_library_fixture()
        inputs = {
            "pdf_path": source,
            "subject": "math",
            "grade": "2",
            "semester": "upper",
            "edition_id": "pep-math-2a-2024",
            "title": "人教版数学二年级上册",
            "publisher": "人民教育出版社",
            "edition_label": "2024版",
            "edition_notes": "依据版权页登记版次。",
            "redistribution_evidence": "https://example.invalid/permission/123",
            "confirm_public_redistribution": True,
        }

        with self.assertRaisesRegex(validator.CatalogError, "repository 不一致"):
            action_input.process(
                inputs,
                root=root,
                github_ref_name="main",
                github_repository="another-owner/another-library",
            )

        self.assertTrue((root / source).is_file())
        self.assertEqual(validator.load_catalog(root)["books"], [])


if __name__ == "__main__":
    unittest.main()
