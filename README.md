# TextbookLibrary

A standalone, source-agnostic library for organizing public textbook files by
subject, grade, semester and edition. The repository contains its own catalog
schema and validation tools. It does not depend on any learning app.

The repository, all files and every Git commit are public. `downloadable: false`
only hides an entry from consumers; it does not restrict direct access to its
file. The MIT license in this repository covers the tooling only, not textbook
content.

## Layout

```text
catalog.json
catalog.schema.json
books/<subject>/grade-<n>/<upper|lower>/<original-filename>.pdf
covers/<pdf-sha256>.webp
tools/validate_catalog.py
tools/requirements.txt
tests/test_validate_catalog.py
.github/workflows/process-textbook.yml
```

## Add a book through GitHub

Upload the PDF with its original filename into the correct subject/grade/semester
directory on `main`, then run **Actions → Process textbook catalog → Run
workflow**. The `auto` choice finds the most recent commit that changed PDFs
and processes its unregistered PDF files, even if later code-only commits exist.
It reads subject, grade and semester from each directory,
generates edition IDs from paths, reads edition labels from filenames, and
extracts titles, publishers and evidence pages from the first eight PDF pages.
If no PDF has ever been committed, `auto` falls back to the only unregistered
PDF in the repository. Select one item from the dropdown to process just that
book, or use `pdf_path` for a PDF not listed there. Dropdown choices are static
and need a workflow-file update only when you want a new book to appear as a
named choice; they are not needed for `auto` batch processing.

When processing a batch, leave the single-book metadata overrides (`edition_id`,
`title`, `publisher`, `edition_label`, `edition_notes`, optional publication
facts and `evidence_pdf_pages`) blank. Scanned PDFs or books with an unrecognized
layout can be processed individually with manual overrides.

Redistribution information is optional and does not block processing or catalog
validation. You can leave `redistribution_evidence` blank and
`confirm_public_redistribution` unchecked, including in `auto` batch mode.
The catalog records `redistributionPermissionVerified: false` unless both a
confirmation and a nonempty evidence reference are provided. Existing
verification records are preserved. The workflow renders covers, extracts PDF
metrics, validates the fixed source URLs, updates `catalog.json`, and pushes
the generated catalog change. Original filenames are preserved in `sourcePath`.

`upper` means 上册 and `lower` means 下册. The path must follow
`books/<subject>/grade-<n>/<upper|lower>/<filename>.pdf`.

Example paths:

```bash
books/math/grade-02/upper/数学 人教版 二年级 上册 2024版.pdf
books/math/grade-02/lower/数学 人教版 二年级 下册 2022版.pdf
```

Optional publication/printing facts remain null when unknown. The PDF path
supplies its subject, grade and semester, so these values do not need to be
entered twice.

The Action runs only on `main` and needs `contents: write` to push the generated
catalog. If repository policy blocks workflow writes, allow write access for
`GITHUB_TOKEN` under Settings → Actions → General → Workflow permissions.

## Local fallback

The `add` command supports the same metadata flow locally. It preserves the
original PDF name and requires the explicit `--commit-assets` flag.
`--redistribution-evidence` and `--confirm-public-redistribution` are optional.

To validate the complete catalog at any time:

```bash
python3 tools/validate_catalog.py check
```

For local setup, create a virtual environment and install
`tools/requirements.txt`. The local validator never pushes files; the GitHub
Action pushes catalog updates using its scoped `GITHUB_TOKEN`.
