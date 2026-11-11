#!/usr/bin/env python3
"""SVG ground truth for icons: rasterize an SVG at each size with real
Chromium (the engine a browser tab uses; SVG is rasterized AT the requested
size, which is exactly what a tab does), transparent background. Lives in
the repo (not /tmp — /tmp gets cleaned and the toolchain must not die with
it). Run with the portal venv python (playwright).

  python .venv/bin/python tools/icon_svg.py raster SVG [--sizes 16,32,...]

Writes SVGdir/svg/<stem>/<stem>-<size>.png (+ manifest.json). The sheet and
dialog-context validators from the icon arc live beside this in the portal
journal record (6.14.1zv); a compact sheet builder is included for the
tab-size proof:

  python3 tools/icon_svg.py sheet SVG LABEL       (system python3, PIL)
"""
import base64
import json
import pathlib
import sys

TMP = pathlib.Path("/tmp/opencode/icons")
SIZES = [256, 128, 64, 32, 16]


def raster(svg: pathlib.Path, sizes=None) -> pathlib.Path:
    from playwright.sync_api import sync_playwright
    sizes = sizes or SIZES
    outdir = TMP / "svg" / svg.stem
    outdir.mkdir(parents=True, exist_ok=True)
    markup = svg.read_text()
    renders = {}
    with sync_playwright() as p:
        b = p.chromium.launch()
        for s in sizes:
            pg = b.new_page(viewport={"width": s, "height": s})
            doc = ("<style>html,body{margin:0;padding:0;background:transparent;}"
                   f"svg{{display:block;width:{s}px;height:{s}px}}</style>" + markup)
            # inline in a data: URL: set_content pages are chrome:// origins
            # and refuse file:// subresources (the raster comes out as the
            # broken-image glyph — discovered live, do not regress)
            pg.goto("data:text/html;base64," +
                    base64.b64encode(doc.encode()).decode())
            pg.wait_for_timeout(250)
            out = outdir / f"{svg.stem}-{s}.png"
            pg.screenshot(path=str(out), omit_background=True)
            renders[str(s)] = str(out)
            pg.close()
        b.close()
    m = outdir / "manifest.json"
    m.write_text(json.dumps(renders))
    print(f"raster: {svg.name} -> {m}")
    return m


def sheet(svg: pathlib.Path, label: str = "") -> pathlib.Path:
    from PIL import Image, ImageDraw
    manifest = TMP / "svg" / svg.stem / "manifest.json"
    renders = {int(k): pathlib.Path(v)
               for k, v in json.loads(manifest.read_text()).items()}
    ims = {s: Image.open(p).convert("RGBA") for s, p in renders.items()}
    sizes = sorted(ims, reverse=True)
    W = sum(sizes) + 30 * len(sizes) + 20
    plate = max(sizes)
    H = 2 * plate + 20 + 40 + 30 + 2 * 90 + 20
    canvas = Image.new("RGBA", (W, H), (24, 26, 30, 255))
    dr = ImageDraw.Draw(canvas)
    dr.text((20, 8), f"SVG native: {label or svg.stem}", fill=(230, 237, 243, 255))
    for plate_col, y in (((22, 27, 34, 255), 40), ((246, 248, 250, 255), 40 + plate + 20)):
        pl = Image.new("RGBA", (W, plate), plate_col)
        x = 20
        for s in sizes:
            pl.alpha_composite(ims[s], (x + (plate - s) // 2, (plate - s) // 2))
            x += s + 30
        canvas.alpha_composite(pl, (0, y))
        dr = ImageDraw.Draw(canvas)
        dr.text((20, y + plate + 2), "dark plate" if plate_col[0] < 100 else "light plate",
                fill=(154, 167, 179, 255))
    g16 = ims.get(16) or ims[min(ims)]
    ty = 40 + 2 * (plate + 20) + 14
    for tab, txt, name in (((22, 27, 34, 255), (201, 209, 217, 255), "dark tab"),
                           ((255, 255, 255, 255), (31, 41, 51, 255), "light tab")):
        dr.rounded_rectangle((20, ty, 260, ty + 34), 8, fill=tab)
        canvas.alpha_composite(g16, (32, ty + 9))
        dr.text((56, ty + 9), f"HALO \u2014 {name}", fill=txt)
        ty += 90
    dst = TMP / f"sheet_svg_{svg.stem}.png"
    canvas.save(dst)
    print(f"sheet: {dst}")


if __name__ == "__main__":
    if sys.argv[1] == "raster":
        sizes = None
        if "--sizes" in sys.argv:
            sizes = [int(x) for x in sys.argv[sys.argv.index("--sizes") + 1].split(",")]
        raster(pathlib.Path(sys.argv[2]), sizes)
    else:
        sheet(pathlib.Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "")
