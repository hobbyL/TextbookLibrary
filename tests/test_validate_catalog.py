from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
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

    def test_valid_committed_pdf_and_cover_pass(self) -> None:
        warnings = validator.validate_catalog(self.root)
        self.assertEqual(warnings, [])

    def test_downloadable_book_requires_redistribution_evidence(self) -> None:
        catalog = validator.load_catalog(self.root)
        catalog["books"][0]["redistributionPermissionVerified"] = False
        (self.root / "catalog.json").write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(validator.CatalogError, "再分发核验记录"):
            validator.validate_catalog(self.root)

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


if __name__ == "__main__":
    unittest.main()
