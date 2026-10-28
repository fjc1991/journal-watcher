#!/usr/bin/env python3
"""End-to-end download check: runs the real cascade (Unpaywall → Elsevier API →
direct → background browser) on known papers from every publisher, saving into
a temporary folder so the library and Papers/_index.csv are untouched.

    python test_downloads.py              # all cases
    python test_downloads.py DSS PNAS     # only these journals

"expect" is what should happen WITHOUT publisher sign-ins: ok, or needs_login
(paywalled). After signing in via "Publisher logins", needs_login cases for that
publisher should turn into ok.
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import download as dl  # noqa: E402

CASES = [
    # abbr, publisher, doi, url, expect, title
    ("ISR", "informs", "10.1287/isre.2023.ed.v34.n2", None, "ok", "Janus Effect of GenAI (free editorial)"),
    ("ISR", "informs", "10.1287/isre.2024.1361", None, "ok|needs_nova", "GenAI and OSS (via Nova EBSCO)"),
    ("MS", "informs", "10.1287/mnsc.2025.00954", None, "ok", "From Bytes to Bites (INFORMS membership)"),
    ("MISQ", "aisel", "10.25300/MISQ/2026/17971", "https://aisel.aisnet.org/misq/vol50/iss3/5", "ok|needs_nova",
     "FAIR design theory (via Nova EBSCO)"),
    ("MSOM", "informs", "10.1287/msom.2024.0805", None, "ok|needs_nova", "Platform Compatibility (via Nova EBSCO)"),
    ("JMIS", "tandfonline", "10.1080/07421222.2023.2172774", None, "ok|needs_login", "JMIS article (Nova IP access)"),
    ("ISJ", "wiley", "10.1111/isj.70060", None, "ok", "Freeriding and Rebellion (open access)"),
    ("DSS", "elsevier", "10.1016/j.dss.2026.114752",
     "https://www.sciencedirect.com/science/article/pii/S0167923626001417", "ok", "Privacy risk framework"),
    ("CACM", "acm", "10.1145/3769433", None, "ok", "Economics of Transformative AI (web-only)"),
    ("DSS", "elsevier", "10.1016/j.dss.2026.114750", "https://doi.org/10.1016/j.dss.2026.114750", "ok",
     "TORA via DOI link (archive-search style URL)"),
    ("SCI", "aaas", "10.1126/science.aeg6779", None, "ok|unavailable",
     "The Virtual Biotech: A multi-agent AI framework for therapeutic discovery and development"),
    ("PNAS", "pnas", "10.1073/pnas.2531748123", None, "ok",
     "AI-generated language remains systematically distinguishable from human communication"),
    ("PNAS", "pnas", "10.1073/pnas.2534042123", None, "ok|unavailable",
     "Efficient and accurate steering of large language models through attention-guided feature learning"),
    ("PNAS", "pnas", "10.1073/pnas.1718942115", None, "ok", "2018 PNAS paper (free after 6 months)"),
    ("NBER", "nber", "10.3386/w35905", None, "ok", "Credit Markets with New Screening Technologies"),
    ("HBR", "hbr", None, "https://hbr.org/2026/10/leaders-need-to-grapple-with-employee-ambivalence-on-ai",
     "ok", "HBR article page → PDF"),
]


def main():
    only = set(a.upper() for a in sys.argv[1:])
    cases = [c for c in CASES if not only or c[0] in only]
    tmp = tempfile.mkdtemp(prefix="jw-dltest-")
    dl.PAPERS = tmp  # out_path / save_index write here, not into the real library
    env = dl.load_env()
    rows, t_all = [], time.time()
    with dl.open_browser() as br:
        print("browser:", "background (headless)" if br and not br.visible else ("visible" if br else "UNAVAILABLE"))
        for abbr, pub, doi, url, expect, title in cases:
            p = {"journal": abbr, "abbr": abbr, "publisher": pub, "doi": doi, "title": title,
                 "url": url or ("https://doi.org/" + doi if doi else ""), "pub_date": "2026-10"}
            t = time.time()
            try:
                ok, path, note = dl.download_paper(p, env, browser=br)
            except Exception as e:  # a crash is a test failure, not a skip
                ok, path, note = False, None, "CRASH: %r" % e
            got = "ok" if ok else ("needs_nova" if "needs_nova_login" in note else
                                  "unavailable" if "not_in_library" in note else
                                  "needs_login" if "needs_login" in note else
                                  "needs_check" if "challenge:" in note else "failed")
            size = os.path.getsize(path) // 1024 if ok else 0
            rows.append((abbr, title, expect, got, time.time() - t, size, note))
            mark = "PASS" if got in expect.split("|") else ("LOGIN" if expect == "ok" and got == "needs_login" else "FAIL")
            print("%-5s %-6s %-44s expect=%-11s got=%-11s %5.1fs %6s  %s" % (
                mark, abbr, title[:44], expect, got, rows[-1][4], ("%dKB" % size) if ok else "", note[:90]),
                flush=True)
    shutil.rmtree(tmp, ignore_errors=True)
    fails = [r for r in rows if r[3] not in r[2].split("|")]
    print("\n%d/%d as expected in %.0fs." % (len(rows) - len(fails), len(rows), time.time() - t_all))
    for r in fails:
        print("  ✗ %s %s: expected %s, got %s — %s" % (r[0], r[1], r[2], r[3], r[6]))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
