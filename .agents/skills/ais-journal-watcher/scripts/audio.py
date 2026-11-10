"""A short spoken briefing (G12): the AI writes a two-minute script from your paper card (or from the folder's synthesis / cards), and the browser reads it aloud with the
voices already on your computer (the Web Speech API), so the text-to-speech part is free and nothing is uploaded. Here: the script only.

    for m in audio.stream_paper(pdf, meta, fresh): ...      status | text{delta} | done{...} | error{error}      (same shape as explain.stream)
    for m in audio.stream_folder(folder, rows, fresh): ...

The script is written only from what the card / synthesis says, in plain spoken sentences: no headings, lists, brackets or citation numbers.
"""
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai  # noqa: E402
import papercard as pc  # noqa: E402
import relatedwork as rw  # noqa: E402
import synthesis as sy  # noqa: E402

RULES = """Write for the ear, not the page: short plain sentences (most under 20 words), no headings, no bullet points, no brackets, no citation numbers, no abbreviations that are hard to say aloud, no Markdown.
Speak to one researcher, warmly and directly ("Here is what this paper does…"). Say numbers the way you would say them. Use only what the notes say: never add a result, a number or a claim that is not there, and say plainly when something is not known.
Paragraphs are separated by a blank line. Do not say "script" or "briefing", and do not greet or sign off beyond one opening and one closing sentence. About 320 words in all."""
PAPER = """You write the script of a short spoken briefing about ONE academic paper, from its structured notes, for a researcher who is listening, not reading.
Cover, in this order: the question and why it matters; how the authors studied it (data and method, in a sentence or two); what they found; what is new compared with earlier work; what to be careful about (limits). End with one question the researcher could think about next.
""" + RULES
FOLDER = """You write the script of a short spoken briefing about a SET of academic papers, from structured notes, for a researcher who is listening, not reading.
Cover, in this order: what the papers are about together; what they agree on; where they disagree and why (data, setting, method); what is still open. Name the papers by their authors and year the first time, then briefly. End with one question the researcher could think about next.
""" + RULES


def _stream(system, material, fresh):
    for kind, v in ai.stream("audio", [{"role": "user", "content": material}], system=system, max_tokens=2200, cache=True, fresh=fresh):
        if kind == "text":
            yield {"type": "text", "delta": v}
        else:
            yield {"type": "done", "provider": v.provider, "model": v.model, "cost": v.cost, "cached": v.cached, "cut_off": v.cut_off}


def stream_paper(pdf, meta, fresh=False):
    try:
        card = pc.load(pdf)
        if card:
            material = "PAPER: %s\n\n%s" % (meta.get("title", ""), pc.markdown(card, "")[:9000])
        else:
            md = pdf[:-4] + ".md"
            if not os.path.exists(md):
                yield {"type": "error", "error": "Make the paper's card (or its summary) first: the briefing is written from it."}
                return
            material = "PAPER: %s\n\nSUMMARY\n\n%s" % (meta.get("title", ""), open(md, encoding="utf-8").read()[:9000])
        yield {"type": "status", "msg": "Writing the briefing…"}
        yield from _stream(PAPER, material, fresh)
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}


def stream_folder(f, rows, fresh=False):
    try:
        doc = sy.load(f["id"])
        if doc:
            th = "\n".join("- %s (%s): %s%s" % (t["name"], t["consensus"], t["summary"], " Where they differ: " + t["tension"] if t["tension"] else "") for t in doc["themes"])
            gp = "\n".join("- %s (%s). Why it matters: %s" % (g["gap"], g["kind"], g["why"]) for g in doc["gaps"])
            material = "FOLDER: %s\n\nPAPERS: %s\n\nOVERVIEW\n%s\n\nTHEMES\n%s\n\nWHAT IS STILL OPEN\n%s" % (f["name"], "; ".join(p["short"] for p in doc["papers"].values()), re.sub(r"\[?P\d+-\d+\]?", "", doc["overview"]), th, gp)
        else:
            papers, _ = rw.gather(rows, set())
            if len(papers) < 2:
                yield {"type": "error", "error": "Make the folder's Synthesis (or at least two paper cards) first: the briefing is written from it."}
                return
            material = "FOLDER: %s\n\nNOTES ON THE PAPERS\n\n%s" % (f["name"], rw.digest(papers)[:14000])
        yield {"type": "status", "msg": "Writing the briefing…"}
        yield from _stream(FOLDER, material, fresh)
    except ai.AIError as e:
        yield {"type": "error", "error": str(e)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        yield {"type": "error", "error": "Something went wrong: %s" % re.sub(r"\s+", " ", str(e))[:200]}
