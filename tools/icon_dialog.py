#!/usr/bin/env python3
"""Dialog-context validation (owner's method, 6.14.1zv): render an icon set
INSIDE realistic app surfaces, not flat plates. Plates hide two failures:
faded (dark edges dissolve into a dark dialog) and harsh (opaque edge +
strong glow stings on a light dialog). Surfaces are the portal's own dark
dialog (#171c24 card on #0e1116) and typical light dialogs (white, mica).
Contexts: splash 128, window title 16/24, list rows 32/48, plus a 10x zoom
of the 48px edge band to inspect the transition itself.

  python3 tools/icon_dialog.py SETDIR      (needs icon-16/24/32/48/128)
"""
import pathlib
import sys
from PIL import Image, ImageDraw, ImageFont

SURFACES = [
    ("portal dark dialog  #171c24", (23, 28, 36, 255), (35, 42, 53, 255),
     (230, 233, 239, 255), (139, 149, 167, 255)),
    ("page dark  #0e1116", (14, 17, 22, 255), (35, 42, 53, 255),
     (230, 233, 239, 255), (139, 149, 167, 255)),
    ("light dialog  #ffffff", (255, 255, 255, 255), (216, 222, 228, 255),
     (31, 41, 51, 255), (89, 99, 110, 255)),
    ("mica dialog  #f3f3f3", (243, 243, 243, 255), (208, 208, 208, 255),
     (27, 29, 32, 255), (96, 100, 106, 255)),
]


def font(sz):
    return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", sz)


def main(setdir: pathlib.Path, out: pathlib.Path):
    def glyph(s):
        return Image.open(setdir / f"icon-{s}.png").convert("RGBA")

    W, H = 4 * 300 + 60, 430
    canvas = Image.new("RGBA", (W, H + 330), (11, 13, 17, 255))
    dr = ImageDraw.Draw(canvas)
    dr.text((16, 8), f"dialog-context check \u2014 {setdir.name}",
            fill=(230, 237, 243, 255), font=font(15))
    x = 16
    for label, card, line, fg, mut in SURFACES:
        dr.rounded_rectangle((x, 48, x + 280, 428), 12, fill=card, outline=line, width=1)
        dr.text((x + 14, 26), label, fill=(154, 167, 179, 255), font=font(13))
        canvas.alpha_composite(glyph(128), (x + 76, 70))
        dr.text((x + 106, 204), "HALO", fill=fg, font=font(22))
        dr.text((x + 72, 234), "the machine, honest", fill=mut, font=font(13))
        canvas.alpha_composite(glyph(16), (x + 16, 276))
        dr.text((x + 40, 276), "16px title", fill=mut, font=font(13))
        canvas.alpha_composite(glyph(24), (x + 140, 272))
        dr.text((x + 172, 276), "24px", fill=mut, font=font(13))
        canvas.alpha_composite(glyph(32), (x + 16, 316))
        dr.text((x + 60, 322), "32px row: llama.cpp build", fill=fg, font=font(14))
        canvas.alpha_composite(glyph(48), (x + 16, 364))
        dr.text((x + 78, 378), "48px row: model download", fill=fg, font=font(14))
        x += 300
    for i, card in enumerate([(23, 28, 36, 255), (255, 255, 255, 255)]):
        g = glyph(48)
        plate = Image.new("RGBA", (48, 48), card)
        plate.alpha_composite(g, (0, 0))
        crop = plate.crop((6, 0, 30, 24)).resize((240, 240), Image.NEAREST)
        canvas.alpha_composite(crop, (16 + i * 260, H + 46))
        dr.text((16 + i * 260, H + 20), "48px edge 10x", fill=(154, 167, 179, 255),
                font=font(12))
    canvas.save(out)
    print(out)


if __name__ == "__main__":
    d = pathlib.Path(sys.argv[1])
    main(d, pathlib.Path(f"/tmp/opencode/icons/dialog_check_{d.name}.png"))
