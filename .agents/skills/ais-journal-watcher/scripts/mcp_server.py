#!/usr/bin/env python3
"""Your paper library as a Model Context Protocol (MCP) server, so Claude Desktop, Claude Code or any MCP client can look things up in it.

    python .agents/skills/ais-journal-watcher/scripts/mcp_server.py          (speaks MCP over stdin/stdout; log lines go to stderr)

READ-ONLY and LOCAL: it reads Papers/ and the machine data next to it; it never writes, never calls an AI model, never touches the network, and only ever opens files inside Papers/.
Tools: search_library, get_paper, read_paper_text, search_highlights, list_folders, folder_papers, get_notes, library_stats.
No third-party package is needed: the protocol (JSON-RPC 2.0, one message per line) is small enough to speak directly.

Claude Desktop (claude_desktop_config.json) or Claude Code (claude mcp add):
    {"mcpServers": {"journal-watcher": {"command": "<python of this project's .venv>", "args": ["<project>/.agents/skills/ais-journal-watcher/scripts/mcp_server.py"]}}}
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import blocks as bl  # noqa: E402
import exports as ex  # noqa: E402
import folders as fo  # noqa: E402
import library_search as ls  # noqa: E402
import notes_store as ns  # noqa: E402
import papercard as pc  # noqa: E402
import pipeline as pl  # noqa: E402

PROTOCOL = "2024-11-05"
MAX_TEXT = 60000


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def rows():
    return pl.library()


def find(file_or_title):
    """A library row by its file path, or by a distinctive part of its title."""
    q = str(file_or_title or "").strip()
    rs = rows()
    r = next((r for r in rs if r["file"] == q), None)
    if r:
        return r
    ql = q.lower()
    hits = [r for r in rs if ql and ql in (r.get("title") or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise ValueError("More than one paper matches “%s”: %s. Use the file path." % (q, "; ".join(h["title"][:60] for h in hits[:5])))
    raise ValueError("No paper in the library matches “%s”. Use search_library first." % q)


def cite(r):
    a = [x.strip() for x in re.split(r";", str(r.get("authors") or "")) if x.strip()]
    return "%s%s (%s)" % ((a[0].split()[-1] if a else "Anon."), " et al." if len(a) > 1 else "", r.get("year") or "n.d.")


def brief(r):
    st = r.get("state") or {}
    return {"file": r["file"], "title": r.get("title", ""), "authors": r.get("authors", ""), "journal": r.get("journal", ""), "year": r.get("year", ""), "doi": r.get("doi", ""),
            "status": st.get("status", ""), "starred": bool(st.get("star")), "takeaway": st.get("takeaway", ""), "folders": r.get("folders", []), "has_card": bool(r.get("card")), "has_summary": bool(r.get("summary"))}


# ---------------------------------------------------------------- tools

def t_search_library(a):
    hits = ls.search(rows(), a.get("query", ""), status=a.get("status", ""), star=bool(a.get("starred")), folder=a.get("folder", ""), journal=a.get("journal", ""), year=str(a.get("year", "") or ""), notes=bool(a.get("with_notes")))
    lim = max(1, min(50, int(a.get("limit") or 10)))
    out = []
    for h in hits[:lim]:
        r = next((x for x in rows() if x["file"] == h["file"]), {})
        out.append({**brief(r), "matches": [{"in": s.get("kind"), "page": s.get("page"), "text": s.get("text")} for s in h.get("snippets", [])]} if r else h)
    return {"total": len(hits), "shown": len(out), "papers": out}


def t_get_paper(a):
    r = find(a.get("paper"))
    out = brief(r)
    pdf = ns.pdf_path(r["file"])
    md = os.path.join(pl.ROOT, r["file"][:-4] + ".md")
    if os.path.exists(md):
        out["summary_markdown"] = open(md, encoding="utf-8").read()[:12000]
    card = pc.load(pdf) if pdf else None
    if card:
        out["card_markdown"] = pc.markdown(card, r.get("title", ""))[:16000]
    n = ns.load(pdf) if pdf else None
    if n:
        out["my_notes"] = {"highlights": len(n.get("highlights", [])), "cards": len(n.get("cards", [])), "paper_note": n.get("paper_note", "")}
    return out


def t_read_paper_text(a):
    r = find(a.get("paper"))
    pdf = ns.pdf_path(r["file"])
    if not pdf:
        raise ValueError("The PDF is not available.")
    p0, p1 = int(a.get("from_page") or 1), int(a.get("to_page") or int(a.get("from_page") or 1) + 4)
    parts, total = [], 0
    for b in bl.extract(pdf):
        pg = b["page"] + 1
        if p0 <= pg <= p1 and b.get("kind", "body") != "ref":
            parts.append("[p. %d] %s" % (pg, b["text"]))
            total += len(b["text"])
            if total > MAX_TEXT:
                parts.append("[…cut: ask for fewer pages]")
                break
    return {"paper": r["title"], "pages": [p0, p1], "text": "\n\n".join(parts) or "(no text in these pages)"}


def t_search_highlights(a):
    res = ls.highlights(rows(), a.get("query", ""), folder=a.get("folder") or None, limit=max(1, min(100, int(a.get("limit") or 20))))
    return {"total": res["n"], "highlights": [{"paper": h["title"], "file": h["file"], "page": h["page"], "kind": h["kind"], "text": h["text"], "my_comment": h["comment"], "colour": h["color"]} for h in res["items"]]}


def t_list_folders(a):
    return {"folders": [{"id": f["id"], "name": f["name"], "papers": f["count"]} for f in fo.list_all()]}


def t_folder_papers(a):
    f = next((f for f in fo.list_all() if f["id"] == a.get("folder") or f["name"].lower() == str(a.get("folder") or "").lower()), None)
    if not f:
        raise ValueError("No such folder. Use list_folders.")
    return {"folder": f["name"], "papers": [brief(r) for r in rows() if f["id"] in (r.get("folders") or [])]}


def t_get_notes(a):
    r = find(a.get("paper"))
    return {"paper": r["title"], "notes_markdown": ex.notes_markdown([r])[:30000]}


def t_library_stats(a):
    rs = rows()
    st = lambda r: (r.get("state") or {}).get("status", "")  # noqa: E731
    return {"papers": len(rs), "with_card": sum(1 for r in rs if r.get("card")), "with_summary": sum(1 for r in rs if r.get("summary")), "with_my_notes": sum(1 for r in rs if r.get("notes")),
            "to_read": sum(1 for r in rs if st(r) == "toread"), "reading": sum(1 for r in rs if st(r) == "reading"), "read": sum(1 for r in rs if st(r) == "read"), "folders": len(fo.list_all())}


S = lambda **p: {"type": "object", "properties": p, "additionalProperties": False}  # noqa: E731
STR = lambda d: {"type": "string", "description": d}  # noqa: E731
INT = lambda d: {"type": "integer", "description": d}  # noqa: E731
TOOLS = [
    ("search_library", "Search the paper library by words in titles, authors, summaries, paper cards and the user's own highlights and notes; optional filters. Returns papers with where the words matched.", t_search_library,
     S(query=STR("words to look for"), status=STR("toread | reading | read | none"), starred={"type": "boolean"}, folder=STR("folder id"), journal=STR("journal abbreviation, e.g. ISR"), year=STR("publication year"), with_notes={"type": "boolean", "description": "only papers the user marked up"}, limit=INT("at most this many (default 10)"))),
    ("get_paper", "Everything known about one paper: metadata, reading status and takeaway, the AI summary and the structured paper card (question, theories, hypotheses, method, findings, limitations) when they exist.", t_get_paper, S(paper=STR("the file path from search_library, or a distinctive part of the title"))),
    ("read_paper_text", "The text of some pages of a paper's PDF (page numbers shown), for reading the paper itself. Default: five pages from from_page.", t_read_paper_text, S(paper=STR("file path or distinctive title"), from_page=INT("first page (1-based)"), to_page=INT("last page"))),
    ("search_highlights", "The user's own highlights and comments across all papers, optionally filtered by words and folder.", t_search_highlights, S(query=STR("words in the highlight or comment"), folder=STR("folder id"), limit=INT("at most this many"))),
    ("list_folders", "The user's folders (virtual lists of papers) with their sizes.", t_list_folders, S()),
    ("folder_papers", "The papers in one folder.", t_folder_papers, S(folder=STR("folder id or name"))),
    ("get_notes", "The user's highlights, comments and notes on one paper, as Markdown.", t_get_notes, S(paper=STR("file path or distinctive title"))),
    ("library_stats", "How big the library is and how much of it is read, summarised and marked up.", t_library_stats, S()),
]
BY_NAME = {t[0]: t for t in TOOLS}


# ---------------------------------------------------------------- the protocol

def handle(msg):
    """One JSON-RPC message in, the reply (a dict) or None for a notification."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                                                        "serverInfo": {"name": "journal-watcher", "version": "1.0"},
                                                        "instructions": "The user's local library of academic papers (information systems). Start with search_library or library_stats; get_paper gives the summary and structured card; read_paper_text reads the PDF itself. Read-only."}}
    if method in ("notifications/initialized", "notifications/cancelled") or (method or "").startswith("notifications/"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": n, "description": d, "inputSchema": sch, "annotations": {"readOnlyHint": True, "openWorldHint": False}} for n, d, _, sch in TOOLS]}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in BY_NAME:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "Unknown tool: %s" % name}}
        try:
            res = BY_NAME[name][2](args if isinstance(args, dict) else {})
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=1)}], "isError": False}}
        except (ValueError, KeyError, TypeError) as e:
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": str(e.args[0] if e.args else e)[:500]}], "isError": True}}
        except Exception as e:  # noqa: BLE001
            log("tool error", name, repr(e))
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}], "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}
    return None


def main():
    log("journal-watcher MCP server: %d tools, library at %s" % (len(TOOLS), pl.PAPERS))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}), flush=True)
            continue
        for m in (msg if isinstance(msg, list) else [msg]):
            reply = handle(m) if isinstance(m, dict) else None
            if reply is not None:
                print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
