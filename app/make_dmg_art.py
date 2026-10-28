"""Draws the picture behind the icons in the disk image (an arrow from the app to Applications), 1x and 2x, as one TIFF.   python make_dmg_art.py <outdir>"""
import os
import subprocess
import sys

from playwright.sync_api import sync_playwright

W, H = 660, 350
HTML = """<style>
html,body{margin:0;width:%dpx;height:%dpx;overflow:hidden;font-family:-apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif}
body{background:linear-gradient(180deg,#fcfbf8 0%%,#f1eee6 100%%);color:#201e1b;position:relative}
h1{position:absolute;left:0;right:0;top:22px;margin:0;text-align:center;font-size:23px;font-weight:700;letter-spacing:-.01em}
p{position:absolute;left:0;right:0;top:56px;margin:0;text-align:center;font-size:14px;color:#6a655d}
.foot{position:absolute;left:0;right:0;bottom:16px;text-align:center;font-size:12.5px;color:#7b766d}
svg{position:absolute;left:0;top:0}
</style>
<h1>Install Journal Watcher</h1><p>Drag the app onto the Applications folder.</p>
<svg width="%d" height="%d" viewBox="0 0 %d %d" fill="none" stroke="#201e1b" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
<path d="M256 160 H 392" stroke-dasharray="1 11" stroke-width="4" opacity=".5"/><path d="M382 145 L 400 160 L 382 175" stroke-width="4" opacity=".8"/>
</svg>
<div class="foot">Then open it from Launchpad or the Applications folder.</div>""" % (W, H, W, H, W, H)


def main(out):
    os.makedirs(out, exist_ok=True)
    pngs = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        for scale in (1, 2):
            pg = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=scale)
            pg.set_content(HTML)
            p = os.path.join(out, "dmg-bg%s.png" % ("" if scale == 1 else "@2x"))
            pg.screenshot(path=p)
            for dpi_png in (p,):
                subprocess.run(["/usr/bin/sips", "-s", "dpiWidth", str(72 * scale), "-s", "dpiHeight", str(72 * scale), dpi_png], check=True, capture_output=True)
            pngs.append(p)
        b.close()
    tiff = os.path.join(out, "dmg-bg.tiff")
    subprocess.run(["/usr/bin/tiffutil", "-cathidpicheck", pngs[0], pngs[1], "-out", tiff], check=True, capture_output=True)
    print("dmg art:", tiff)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "build"))
