"""The page background: a tileable canvas texture and two halo treatments.

The owner's brief, learned the expensive way (2026-10-30/31):
"subtle not invisible" - the first pass at hairline alpha was invisible at
real rendering, and invisible art is decoration in the source tree only.
And the tone rule: "keep the backgrounds close to the same color as the
background of that element... the average tone of the texture should be
close enough to the background color it will be covering that it doesn't
visually merge with different areas on the page" - clarified 2026-10-31:
"it doesn't have to be the exact average, just close enough that it doesn't
fall into the space of the other background colors." Hue is free; the
*average tone of the tile* is the bounded quantity. That is the arithmetic
below, in sRGB units:

  * the tile's area-weighted average tone, composited over the canvas
    (--bg #0e1116), must stay within 30% of the canvas->panel gap (the
    panel is the nearest other background, 14 units away), so the textured
    canvas always reads as canvas and never as a swarm of ghost panels;
  * no paint may land on the panel's tone (the boundary test: card edges
    over the texture must keep their edge);
  * the tile's colors are palette members (the tile speaks the site's
    colors - the same contract the marks' color rules enforce);
  * the tile is square and tiles at its declared size (width == height ==
    viewBox), because a non-square tile shows its seams;
  * base.html references the tile with background-attachment fixed (the
    texture belongs to the viewport, not the scroll - scrolling content
    must not slide a pattern across the gutters) and carries both halo
    treatments the owner picked (nav brand halo, hero radial).
"""
from __future__ import annotations

import math
import pathlib
import re

BASE = pathlib.Path(__file__).resolve().parents[1]
TILE = BASE / "web" / "static" / "portal-bg.svg"
BASE_HTML = (BASE / "web" / "templates" / "base.html").read_text()

# the two surfaces the tone rule is measured against
BG = (0x0E, 0x11, 0x16)        # --bg: the canvas the tile covers
PANEL = (0x17, 0x1C, 0x24)     # --panel: what the texture must not merge with
GAP = math.dist(BG, PANEL)     # 14.0 in sRGB units
AVG_MAX = 0.30 * GAP           # the average tone may cross under a third of the gap

PALETTE = {  # the site's own colors; a tile may not introduce a foreign hue
    "#5aa9ff", "#bc8cff", "#d29922", "#3fb950", "#f85149",
    "#232a35", "#8b95a7", "#e6e9ef", "#0e1116", "#171c24",
}


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _paints(svg: str):
    """Every paintable geometry in the tile as (label, composite, area_frac).

    SVG-faithful defaults: a shape's `fill` is BLACK when unspecified (the
    first version of this parser missed that and shipped dark discs inside
    the halo rings), `stroke` is none, `fill="none"` paints nothing, and a
    `<g>` container paints nothing itself. Area fractions come from the
    geometry (stroke area = width x circumference; a disc centered on a
    tile corner counts its in-tile quadrant), so the average is weighted by
    the pixels a viewer actually sees, not by the loudest stroke.
    """
    vb = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg)
    W, H = float(vb.group(1)), float(vb.group(2))
    tile_area = W * H
    for tag in re.finditer(r"<circle\b[^>]*/?>", svg):
        t = tag.group(0)

        def attr(name: str, default: str) -> str:
            m = re.search(name + r'="([^"]+)"', t)
            return m.group(1) if m else default

        cx, cy, r = (float(attr(k, "0")) for k in ("cx", "cy", "r"))
        a = float(attr("opacity", "1"))
        # a shape at a tile corner tiles its outer parts into the neighbors:
        # count only the part inside this tile (quadrant/half factors)
        fx = 0.5 if cx in (0.0, W) else 1.0
        fy = 0.5 if cy in (0.0, H) else 1.0
        f = fx * fy
        for kind, default, area in (
            ("fill", "#000000", math.pi * r * r),
            ("stroke", "none", 2 * math.pi * r * float(attr("stroke-width", "1"))),
        ):
            c = attr(kind, default).lower()
            if c in ("none", "transparent") or not re.fullmatch(r"#[0-9a-f]{6}", c):
                continue
            col = _hex(c)
            comp = tuple(cc * a + b * (1 - a) for cc, b in zip(col, BG))
            yield (f"{kind} {c} a={a}", comp, area * f / tile_area)


def test_the_served_tile_exists_and_tiles_square():
    assert TILE.is_file(), "the served tile is the background; without it base.html 404s"
    svg = TILE.read_text()
    w = re.search(r'width="(\d+)"', svg)
    h = re.search(r'height="(\d+)"', svg)
    vb = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
    assert w and h and vb
    assert w.group(1) == h.group(1) == vb.group(1) == vb.group(2), \
        "a non-square tile shows its seams"


def test_a_corner_halo_is_drawn_at_all_four_corners():
    """A circle centered on ONE tile corner renders only its in-tile
    quadrant; the other three quadrants fall off the tile and tiling leaves
    quarter-arcs that never close (the owner saw "a clipped circle in one
    corner"). The seam-free form: draw the circle at every corner, so the
    four quadrants meet at each tiling junction and close the circle."""
    svg = TILE.read_text()
    W = H = float(re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).group(1))
    corners = {(0.0, 0.0), (W, 0.0), (0.0, H), (W, H)}
    at_corner: dict[tuple, set] = {}
    for tag in re.finditer(r"<circle\b[^>]*/?>", svg):
        t = tag.group(0)
        g = lambda k, d="0": re.search(k + r'="([^"]+)"', t).group(1) if re.search(k + r'="([^"]+)"', t) else d
        cx, cy, r = float(g("cx")), float(g("cy")), float(g("r"))
        if (cx, cy) not in corners:
            continue
        key = (r, g("stroke").lower(), g("opacity"), g("stroke-width"))
        at_corner.setdefault(key, set()).add((cx, cy))
    for key, spots in at_corner.items():
        assert spots == corners, (
            f"corner halo r={key[0]} is drawn at {sorted(spots)} - a corner "
            "circle needs all four corner copies to tile into a full circle")


def test_the_tile_average_tone_stays_in_the_canvases_space():
    """The owner's rule as arithmetic: hue free, the tile's average tone
    close enough to the canvas that it never falls into the panel's space."""
    paints = list(_paints(TILE.read_text()))
    assert paints, "the tile paints nothing"
    total = sum(area for _l, _c, area in paints)
    avg = tuple(sum(comp[i] * area for (_l, comp, area) in paints) / total
                for i in range(3))
    # composite over the unpainted remainder: the tile's seen average tone
    seen = tuple(c * total + b * (1 - total) for c, b in zip(avg, BG))
    d_bg = math.dist(seen, BG)
    d_panel = math.dist(seen, PANEL)
    assert d_bg <= AVG_MAX, (
        f"the tile's average tone sits {d_bg:.1f} from the canvas; the panel is "
        f"{GAP:.1f} away, so the texture is on its way to reading as ghost panels")
    assert d_panel >= GAP - AVG_MAX, (
        "the tile's average tone is closer to the panel than the rule allows")
    # the rule bounds the AVERAGE (owner, 2026-10-31), so individual thin
    # strokes may read brighter - but none may land on the panel's tone:
    # card boundaries over the texture must keep their edge
    for label, comp, _area in paints:
        assert math.dist(comp, PANEL) > 1.5, f"{label} lands on the panel tone"


def test_the_tile_speaks_the_palette():
    svg = TILE.read_text()
    colors = set(re.findall(r'(?:stroke|fill)="(#[0-9a-fA-F]{6})"', svg))
    assert colors, "the tile declares no colors"
    foreign = {c for c in colors if c.lower() not in PALETTE}
    assert not foreign, f"the tile introduces colors the site does not use: {foreign}"


def test_base_wires_the_background_and_the_picked_treatments():
    assert "url(/static/portal-bg.svg) fixed" in BASE_HTML, (
        "the tile must ride the viewport (fixed), not scroll across the gutters")
    assert "nav .brand::before" in BASE_HTML, "the owner picked the brand halo"
    assert ".router-hero{background-image:radial-gradient" in BASE_HTML, \
        "the owner picked the hero radial"


def test_the_banner_moire_stays_in_the_banners_tone_space():
    """The owner's moiré (two ring sets, light + dark, offset) is amplitude-
    bounded by the same tone rule that governs the tile, measured from the
    panel tone the banner is painted on:

      * each ring stop composites within AMAX of the panel tone (light rings
        brighten, dark rings darken; nothing lands on the canvas tone, which
        would read as a hole punched in the banner);
      * the sets' average drift stays under 30% of the canvas->panel gap, so
        the packed pattern beats a texture, not a tinted banner.
    """
    m = re.search(r"nav\{[^}]*background-image:(.*?)\}\s*\n", BASE_HTML, re.S)
    assert m, "the banner moiré is the owner's design; without it the nav is flat"
    css = m.group(1)
    assert css.count("repeating-radial-gradient") == 2, \
        "moiré needs TWO ring sets - one set is rings, not interference"
    amax = 12.0
    stops = []
    for rgba in re.finditer(r"rgba\((\d+),(\d+),(\d+),([0-9.]+)\)", css):
        col = tuple(int(rgba.group(i)) for i in (1, 2, 3))
        a = float(rgba.group(4))
        comp = tuple(c * a + p * (1 - a) for c, p in zip(col, PANEL))
        stops.append((col, a, comp))
    assert len(stops) >= 4, "each set needs a light and a dark stop"
    for col, a, comp in stops:
        d = math.dist(comp, PANEL)
        assert d <= amax, f"stop rgba{col} a={a} composites {d:.1f} from the panel tone"
        assert math.dist(comp, BG) > 3.0, \
            f"stop rgba{col} a={a} lands on the canvas tone: a hole in the banner"
    avg = tuple(sum(c[i] for _col, _a, c in stops) / len(stops) for i in range(3))
    drift = math.dist(avg, PANEL)
    assert drift <= 0.30 * GAP, (
        f"the rings' average tone drifts {drift:.1f} from the panel tone; the "
        "banner stops reading as one banner past "
        f"{0.30 * GAP:.1f}")
