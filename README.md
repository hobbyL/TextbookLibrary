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
books/<subject>/grade-<n>/<upper|lower>/<edition-id>.pdf
covers/<pdf-sha256>.webp
tools/validate_catalog.py
tools/requirements.txt
tests/test_validate_catalog.py
```

## Prepare a book

Create the permanent PDF path, then run:

```bash
python3 tools/validate_catalog.py prepare --pdf books/math/grade-02/upper/example-edition.pdf
```

The command opens the PDF with PDFium, measures its page count and extractable
text, and renders page 1 to a WebP cover. It prints the SHA-256, byte count,
page count, text statistics and cover hash.

After confirming redistribution rights, commit the PDF and generated cover.
Then add a catalog entry containing its subject, grade, semester, title,
publisher, edition facts, `sourcePath`, `coverPath` and permission evidence.
Use a new path for each edition; do not overwrite a published file.

Pin the catalog entry to the commit containing its PDF and cover:

```bash
python3 tools/validate_catalog.py pin
python3 tools/validate_catalog.py check
```

The PDF/cover commit and catalog commit are separate because a commit cannot
contain its own hash. The catalog stores immutable source commit, path, Git
blob SHA, PDF hash, length, page count, text statistics and cover hash.

For local setup, create a virtual environment and install
`tools/requirements.txt`. The validator never uploads or pushes files.
