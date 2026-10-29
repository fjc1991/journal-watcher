# Third-party software

Journal Watcher is licensed under the GNU AGPL v3 or later (`LICENSE`). It uses, and the Mac app bundles, the following. Each keeps its own licence; versions are whatever `requirements.txt` resolves to.

| Component | Used for | Licence |
|---|---|---|
| [PyMuPDF](https://pymupdf.readthedocs.io) | PDF text blocks, annotated-PDF export | AGPL-3.0 (or commercial) — the reason this project is AGPL |
| [pdf.js](https://mozilla.github.io/pdf.js/) (`dashboard/static/vendor/pdfjs`) | PDF rendering in the reader | Apache-2.0 |
| [marked](https://github.com/markedjs/marked) (`dashboard/static/vendor/marked.min.js`) | Markdown in AI answers | MIT |
| [Inter](https://rsms.me/inter/) (loaded from Google Fonts when online) | UI typeface | SIL OFL 1.1 |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | Claude API client | MIT |
| [Playwright for Python](https://playwright.dev/python/) | Sign-in windows and PDF downloads (Chromium) | Apache-2.0 |
| [fastembed](https://github.com/qdrant/fastembed), [ONNX Runtime](https://onnxruntime.ai), BAAI/bge-small-en-v1.5 | Local embeddings for semantic search | Apache-2.0, MIT, MIT |
| [NumPy](https://numpy.org) | Vectors | BSD-3-Clause |
| [python-docx](https://python-docx.readthedocs.io) | Word export | MIT |
| [pywebview](https://pywebview.flowrl.com) | The Mac app's window | BSD-3-Clause |
| [PyInstaller](https://pyinstaller.org) | Packaging (the bootloader is GPL with an exception that allows any licence for the packaged app) | GPL-2.0 with exception |
| [PyNaCl](https://pynacl.readthedocs.io) (libsodium inside) | Checking the signature of the update feed | Apache-2.0 (libsodium: ISC) |
| [dmgbuild](https://github.com/dmgbuild/dmgbuild) | The disk image | MIT |

Journal metadata comes from the publishers' feeds, Crossref, OpenAlex and Unpaywall; use of those services is subject to their terms. The PDFs you download are covered by the publishers' licences and your institution's agreements: they are never part of this repository.
