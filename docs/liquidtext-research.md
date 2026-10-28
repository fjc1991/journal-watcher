# LiquidText research (for the Journal Watcher reader)

Source: the LiquidText 7.3.17 app installed on this Mac (Mac Catalyst build on PSPDFKit), inspected read-only: Info.plist, the names and
stills of its bundled tutorials, `feature-list.html`, user-facing strings in the binary, and the data model of its AI vector index. Nothing was
copied; this is a feature inventory used to decide what to build.

Core idea: **source documents ⟷ a workspace, with everything linked back to its exact place in the source.**

| Area | What LiquidText does | Status in our reader |
|---|---|---|
| Excerpts | Drag text/image from the PDF into the workspace; the excerpt keeps a link button back to the source; group/connect excerpts; freeform excerpt by circling; AutoExcerpt | **Done** (text excerpts + ⤴ link, drag or → Notes). Groups/connectors, freeform: next |
| Collapse ("pinch") | Collapse the document to search hits or highlights plus adjustable context; compare distant pages | **Done** (⇕ Collapse, context slider, gaps expand on click). Side-by-side pages: next |
| Highlights | 32 colours, tags on highlights, HighlightView, edit/remove | **Done** (8 colours, tags, HighlightView, comments) |
| Comments & links | Comments on 1–10 items across documents; link bar; two link buttons show both sources; copy-link URLs | Comments on highlights/notes done; cross-item links next |
| Ink | Pen, highlighter, eraser, ink links, ink boxes (Pro) | Not planned (mouse/trackpad app) |
| Tags | Simple + categorised tags on excerpts/highlights/docs, Tag Manager, search by tag | **Done** (`category: name`, shared across papers) |
| Multi-doc | Many docs per project, parallel view, cross-document search with hit bars | Next (`Papers/_workspaces/`) |
| Workspaces | Nested workspaces, links between them, styles, lasso, overview | Single notes canvas per paper; more next |
| Navigation | Type-to-navigate, bookmarks, outline, history, page editor | **Done** except the page editor |
| Defined terms | Auto-underlined defined terms/exhibits, undefined/over-defined scans | Not planned (legal use case) |
| Import/export | PDF/Word/PPT/web, Zotero/Mendeley, DOCX/PDF/outline export | Markdown export done; annotated PDF, Zotero/BibTeX next |
| Sync/collab | Real-time multi-device sync, collaboration | Not planned |
| OCR | Searchable scanned PDFs | Next if image-only PDFs show up |
| **AI** | **Ask** (`/AI`): paragraphs are embedded into a vector index, the nearest ones go to ChatGPT, the answer cites paragraph numbers; **Auto-Link** (`//`): a typed sentence is linked to the most relevant passage; auto-cite; pay-per-credit | **Ask done** with provider choice, cost shown and citations that jump to the passage. Auto-Link next |

## Where LiquidText is weak (our opportunity)
One AI vendor behind paid credits; paragraph retrieval only (weak on whole-paper questions); no paper-level metadata (journal, theory, method,
constructs); no literature-review workflow; no citation resolution; closed Apple-only format; AI answers are not durable, linkable notes.

## Roadmap beyond LiquidText
1. Provider-agnostic AI with per-feature routing and a cost ledger — **done** (Claude, OpenAI, Gemini, OpenRouter, Ollama).
2. Citation-grounded answers saved as notes — **done**.
3. AI-suggested highlights by research lens (research question, hypothesis, theory, method/sample, finding, limitation) — propose, you accept.
4. Structured paper cards (theory, constructs, hypotheses, method, findings, limitations) with every field cited to a passage; comparison table.
5. Cross-paper synthesis from tagged excerpts (concept matrix, disagreements, draft literature-review paragraph); ask the whole library.
6. Reference resolution: click `[14]`, resolve the bibliography entry, offer to download it with the existing pipeline.
7. `//` Auto-Link and related passages in other papers.
8. Interop: annotated-PDF export (standard highlights), DOCX, BibTeX/RIS, Zotero.
9. Claim check: select a claim, find supporting and contradicting passages in the library.
