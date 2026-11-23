# ADR 004: pymupdf4llm markdown + table-safe chunking

- **Status:** accepted
- **Decision:** PDF pages are read with `pymupdf4llm` (markdown with tables). `chunk_page` keeps paragraphs whole, packs them up to about 120 words, and never cuts a table row; a long table is split between rows and every part repeats the header. Every chunk is searched and embedded together with its document title and date.
- **Fallback:** where PyMuPDF cannot load (Windows Application Control blocks its DLL) `pypdfium2` gives plain text without table layout. This fallback is what ran in testing on this machine, so the markdown path is only unit-tested on synthetic tables here.
