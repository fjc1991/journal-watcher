"""Draws the app icon (a dark rounded square with the library glyph) and writes JournalWatcher.icns.   python make_icon.py <outdir>"""
import os
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright

SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024">
<defs>
 <linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#34312d"/><stop offset="1" stop-color="#171614"/></linearGradient>
 <linearGradient id="h" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff" stop-opacity=".16"/><stop offset=".5" stop-color="#ffffff" stop-opacity="0"/></linearGradient>
 <filter id="s" x="-20%" y="-20%" width="140%" height="150%"><feDropShadow dx="0" dy="14" stdDeviation="16" flood-color="#000" flood-opacity=".35"/></filter>
</defs>
<rect x="100" y="90" width="824" height="824" rx="188" fill="url(#g)" filter="url(#s)"/>
<rect x="100" y="90" width="824" height="824" rx="188" fill="url(#h)"/>
<g transform="translate(512 502) scale(23) translate(-13.5 -12)" fill="#fbfaf7">
 <path d="M5 4h3.5v16H5zM10.5 4H14v16h-3.5zM15.6 5.4l3.3-.9 3 14.2-3.3.9z"/>
</g>
<circle cx="738" cy="296" r="30" fill="#e8a53a"/>
</svg>"""


def main(out):
    os.makedirs(out, exist_ok=True)
    png = os.path.join(out, "icon-1024.png")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1024, "height": 1024})
        pg.set_content("<style>html,body{margin:0;background:transparent}</style>" + SVG)
        pg.screenshot(path=png, omit_background=True)
        b.close()
    iconset = os.path.join(tempfile.mkdtemp(), "JournalWatcher.iconset")
    os.makedirs(iconset)
    for size in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            px = size * scale
            name = "icon_%dx%d%s.png" % (size, size, "@2x" if scale == 2 else "")
            subprocess.run(["/usr/bin/sips", "-z", str(px), str(px), png, "--out", os.path.join(iconset, name)], check=True, capture_output=True)
    subprocess.run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", os.path.join(out, "JournalWatcher.icns")], check=True)
    print("icon:", os.path.join(out, "JournalWatcher.icns"))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__)))
