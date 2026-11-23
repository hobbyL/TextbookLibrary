# TextbookLibrary

A standalone, source-agnostic library for organizing public textbook files by
subject, grade, semester and edition. The repository contains its own catalog
schema and validation tools. It does not depend on any learning app.

Only add a PDF after confirming that it may be redistributed publicly. The
repository, all files and every Git commit are public. `downloadable: false`
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
workflow**. The form takes the PDF path, subject, grade, semester, edition ID,
title, publisher and edition facts. The workflow renders the cover, extracts PDF
metrics, validates the fixed source URL, updates `catalog.json`, and pushes the
generated catalog change. The `editionId` is separate from the original PDF
filename; the filename is preserved in `sourcePath`.

`upper` means 上册 and `lower` means 下册. The selected semester must match the
PDF's directory; the workflow rejects mismatches. No filename change is needed.

Example paths:

```bash
books/math/grade-02/upper/数学 人教版 二年级 上册 2024版.pdf
books/math/grade-02/lower/数学 人教版 二年级 下册 2022版.pdf
```

The workflow requires exact title, publisher, edition label/notes and a
traceable redistribution-evidence reference. Optional publication/printing
facts remain null when unknown. The confirmation input is an explicit curator
assertion; the workflow cannot determine whether the evidence grants permission.
The PDF becomes public as soon as it is uploaded, before the Action runs. Only
upload material authorized for public redistribution. The form rejects a path
whose subject/grade/semester directory conflicts with the submitted fields.

The Action runs only on `main` and needs `contents: write` to push the generated
catalog. If repository policy blocks workflow writes, allow write access for
`GITHUB_TOKEN` under Settings → Actions → General → Workflow permissions.

## Local fallback

The `add` command supports the same metadata flow locally. It preserves the
original PDF name and requires explicit `--confirm-public-redistribution` and
`--commit-assets` flags.

To validate the complete catalog at any time:

```bash
python3 tools/validate_catalog.py check
```

For local setup, create a virtual environment and install
`tools/requirements.txt`. The local validator never pushes files; the GitHub
Action pushes catalog updates using its scoped `GITHUB_TOKEN`.
