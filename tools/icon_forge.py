#!/usr/bin/env python3
"""Icon forge: the HALO icon pipeline. Runs on the system python3
(Pillow + numpy); the portal venv stays lean.

The loop a human runs with their eyes, made scriptable:

  gen NAME PROMPTFILE   render on local ComfyUI (Qwen-Image-2512 fp8 +
                        Lightning-4steps, 4 steps cfg 1 euler/normal —
                        the fast validated lane), on a solid magenta
                        plate so the alpha can be RECOVERED, not guessed.
  key IN OUT            magenta -> alpha by INVERTING the composite
                        (c_true = (c_obs - (1-a)*bg)/a): the anti-aliased
                        edge pixels come back at their true color with
                        their true coverage, which naive "remove white"
                        tools cannot do.
  shrink IN OUTDIR      premultiplied-alpha Lanczos in half-steps
                        (1024->512->...->16): averaging premultiplied
                        color keeps alpha stable — no dark fringing, no
                        halos; straight RGB averaging does both.
  ico SETS OUT.ico      hand-built multi-size ICO (PNG-encoded entries,
                        the Vista+ layout browsers use today).
  sheet IN OUT NAME     the validation surface: the icon at 256/128/64/
                        32/16 on dark AND light plates, plus a browser
                        tab mockup. This is what gets LOOKED AT.

Usage:  python3 tools/icon_forge.py <sub> ...   (see each docstring)
"""
import json
import struct
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

COMFY = "http://127.0.0.1:8188"
WORK = Path("/tmp/opencode/icons")
WORK.mkdir(parents=True, exist_ok=True)

BG = (255, 0, 255)          # the plate the model renders on
QWEN = dict(unet="qwen_image_2512_fp8_e4m3fn.safetensors",
            te="qwen_2.5_vl_7b_fp8_scaled.safetensors",
            vae="qwen_image_vae.safetensors",
            lora="Qwen-Image-2512-Lightning-4steps-V1.0-fp32.safetensors")
ZTURBO = dict(unet="z_image_turbo_bf16.safetensors",
              te="qwen_3_4b.safetensors",
              vae="ae.safetensors",
              lora=None)


# ---------------------------------------------------------------- ComfyUI
def _post(path, payload):
    req = urllib.request.Request(
        COMFY + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def _get(path):
    return json.loads(urllib.request.urlopen(COMFY + path, timeout=60).read())


def workflow(name: str, prompt: str, size: int, seed: int, chain: str = "zturbo") -> dict:
    """Two lanes on local ComfyUI:
      zturbo  Z-Image-Turbo bf16 (12.3 GB) + Qwen3-4B TE + flux VAE,
              euler/normal 8 steps cfg 1 — fits beside the running router.
      q2512   Qwen-Image-2512 fp8 + Lightning-4steps, 4 steps cfg 1 —
              sharper prompt-following; peaked at 37 GB RAM, so it runs
              only when the box has room (the OOM killer has opinions).
    Chain shapes per docs/QWEN_IMAGE_21_RESEARCH.md (cfg 1 lanes: the
    negative is nearly inert; plate discipline lives in the positive)."""
    M = ZTURBO if chain == "zturbo" else QWEN
    steps, sched = (8, "normal") if chain == "zturbo" else (4, "normal")
    unet = {"1": {"class_type": "UNETLoader",
                  "inputs": {"unet_name": M["unet"], "weight_dtype": "default"}}}
    if chain == "zturbo":
        model_from = ["1", 0]
    else:
        unet["1b"] = {"class_type": "LoraLoaderModelOnly",
                      "inputs": {"model": ["1", 0], "lora_name": M["lora"],
                                 "strength_model": 1.0}}
        model_from = ["1b", 0]
    wf = dict(unet)
    wf.update({
        "2": {"class_type": "CLIPLoader",
              "inputs": {"clip_name": M["te"], "type": "qwen_image",
                         "device": "default"}},
        "4": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": prompt}},
        "5": {"class_type": "CLIPTextEncode",
              "inputs": {"clip": ["2", 0], "text": "text, letters, words, watermark, photo, background gradient, drop shadow, frame"}},
        "6": {"class_type": "EmptyLatentImage",
              "inputs": {"width": size, "height": size, "batch_size": 1}},
        "7": {"class_type": "KSampler",
              "inputs": {"model": model_from, "positive": ["4", 0],
                         "negative": ["5", 0], "latent_image": ["6", 0],
                         "seed": seed, "steps": steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": sched,
                         "denoise": 1.0}},
        "8": {"class_type": "VAELoader", "inputs": {"vae_name": M["vae"]}},
        "9": {"class_type": "VAEDecode",
              "inputs": {"samples": ["7", 0], "vae": ["8", 0]}},
        "10": {"class_type": "SaveImage",
               "inputs": {"images": ["9", 0],
                          "filename_prefix": f"iconforge/{name}"}},
    })
    return wf


def gen(name: str, prompt_file: Path, size: int = 768, seed: int = 42,
        chain: str = "zturbo") -> Path:
    prompt = prompt_file.read_text().strip()
    pid = _post("/prompt",
                {"prompt": workflow(name, prompt, size, seed, chain)})["prompt_id"]
    t0 = time.time()
    while True:
        h = _get(f"/history/{pid}")
        if pid in h:
            break
        if time.time() - t0 > 900:
            raise SystemExit("generation timed out (15 min) — is the router hogging the box?")
        time.sleep(1.0)
    node = None
    for out in h[pid]["outputs"].values():
        if isinstance(out, dict) and out.get("images"):
            node = out
            break
    if node is None:
        raise SystemExit(f"no saved image in history for {pid}: "
                         f"{json.dumps(h[pid].get('outputs', {}))[:300]}")
    img = node["images"][0]
    q = urllib.parse.urlencode({"filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    raw = urllib.request.urlopen(f"{COMFY}/view?{q}", timeout=60).read()
    out = WORK / f"{name}.png"
    out.write_bytes(raw)
    secs = round(time.time() - t0, 1)
    print(f"gen {name}: {out} ({size}px seed={seed}) in {secs}s")
    return out


# ---------------------------------------------------------------- alpha
def key(src: Path, dst: Path) -> Path:
    """Plate -> honest alpha. The plate color is MEASURED from the image's
    own border (the VAE renders "pure magenta" a few points off; keying
    against the ideal color leaves an opaque plate — the bug this fixes).
    Then the composite equation c_obs = a*c + (1-a)*P is inverted: alpha
    from the pixel's distance from the plate color, color by unmixing.
    Edges and soft glows keep their real color at their real coverage."""
    import numpy as np
    from PIL import Image
    im = Image.open(src).convert("RGB")
    px = np.asarray(im).astype(np.float64)
    h, w, _ = px.shape
    band = max(2, min(h, w) // 50)
    edge = np.concatenate([px[:band].reshape(-1, 3), px[-band:].reshape(-1, 3),
                           px[:, :band].reshape(-1, 3), px[:, -band:].reshape(-1, 3)])
    P = np.median(edge, axis=0)          # the plate as this render made it
    d = np.sqrt(((px - P) ** 2).sum(-1))
    a = np.clip((d - 25.0) / 70.0, 0.0, 1.0)   # plate..subject band
    aa = a[..., None]
    unmixed = (px - (1.0 - aa) * P) / np.maximum(aa, 1e-6)
    col = np.where(aa > 0.02, unmixed, 0.0)
    col = np.clip(col, 0, 255)
    rgba = np.dstack([col, a * 255.0]).astype(np.uint8)
    # hardening pass: a pixel barely off the plate (a<0.08) is plate
    # noise; clear it so near-transparent dust dies.
    rgba[..., 3][a < 0.08] = 0
    Image.fromarray(rgba, "RGBA").save(dst)
    print(f"key {src.name} -> {dst} ({im.size[0]}px, plate=({P[0]:.0f},"
          f"{P[1]:.0f},{P[2]:.0f}), alpha mean {round(a.mean() * 100)}%)")
    return dst


# ---------------------------------------------------------------- shrink
def _half(im):
    """One half-size step of a premultiplied RGBA image: Lanczos on the
    premultiplied color and on alpha separately, then divide back.
    This is what keeps the alpha channel stable while shrinking."""
    import numpy as np
    from PIL import Image
    a = np.asarray(im).astype(np.float64) / 255.0
    alpha = a[..., 3:]
    prem = np.dstack([a[..., :3] * alpha])
    w, h = im.size
    tw, th = max(1, w // 2), max(1, h // 2)
    pre = Image.fromarray(np.uint8(np.clip(prem * 255.0, 0, 255)))
    al = Image.fromarray(np.uint8(np.clip(alpha[..., 0] * 255.0, 0, 255)))
    pre = pre.resize((tw, th), Image.LANCZOS)
    al = al.resize((tw, th), Image.LANCZOS)
    p = np.asarray(pre).astype(np.float64) / 255.0
    n = np.asarray(al).astype(np.float64) / 255.0
    n3 = n[..., None]
    with np.errstate(divide="ignore", invalid="ignore"):
        col = np.where(n3 > 1e-6, p / n3, 0.0)
    return Image.fromarray(
        np.uint8(np.clip(np.dstack([col * 255.0, n * 255.0]), 0, 255)), "RGBA")


def _resize_prem(im, s):
    """One premultiplied LANCZOS resize to an exact (odd) target."""
    import numpy as np
    from PIL import Image
    a = np.asarray(im).astype(np.float64) / 255.0
    alpha = a[..., 3:]
    prem = np.dstack([a[..., :3] * alpha])
    pre = Image.fromarray(np.uint8(np.clip(prem * 255.0, 0, 255))).resize(
        (s, s), Image.LANCZOS)
    al = Image.fromarray(np.uint8(np.clip(alpha[..., 0] * 255.0, 0, 255))).resize(
        (s, s), Image.LANCZOS)
    p = np.asarray(pre).astype(np.float64) / 255.0
    n = np.asarray(al).astype(np.float64) / 255.0
    n3 = n[..., None]
    with np.errstate(divide="ignore", invalid="ignore"):
        col = np.where(n3 > 1e-6, p / n3, 0.0)
    return Image.fromarray(
        np.uint8(np.clip(np.dstack([col * 255.0, n * 255.0]), 0, 255)), "RGBA")


def shrink(src: Path, outdir: Path, sizes=(16, 24, 32, 48, 64, 128, 180, 192, 256, 512)) -> list:
    """Descending from the master: half-steps while the next half is still
    at or above the target, one premultiplied LANCZOS for odd steps
    (180/192/512). Never upscales — sizes above the master are refused."""
    outdir.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    im = Image.open(src).convert("RGBA")
    w, h = im.size
    if w != h:
        raise SystemExit("icon master must be square")
    cur, out = im, []
    for s in sorted(sizes, reverse=True):
        if s > w:
            print(f"shrink: skip {s} (above master {w})")
            continue
        while cur.size[0] >= 2 * s:
            cur = _half(cur)
        if cur.size[0] != s:
            cur = _resize_prem(cur, s)
        p = outdir / f"icon-{s}.png"
        cur.save(p)
        out.append(p)
        print(f"shrink: {p}")
    return out


# ---------------------------------------------------------------- ico
def build_ico(pngs: list, dst: Path) -> Path:
    """Multi-size ICO with PNG-encoded entries (the layout all current
    browsers read; includes 16x16 — the size generators forget)."""
    blobs = [(Path(p), p.read_bytes()) for p in pngs]
    n = len(blobs)
    out = bytearray(struct.pack("<HHH", 0, 1, n))
    off = 6 + 16 * n
    for p, b in blobs:
        from PIL import Image
        w, h = Image.open(p).size
        out += struct.pack("<BBBBHHII",
                           w % 256, h % 256, 0, 0, 1, 32, len(b), off)
        off += len(b)
    for _, b in blobs:
        out += b
    dst.write_bytes(bytes(out))
    print(f"ico: {dst} ({n} sizes: "
          f"{', '.join(str(Image.open(p).size[0]) for p, _ in blobs)})")
    return dst


# ---------------------------------------------------------------- sheet
def sheet(src: Path, dst: Path, label: str) -> Path:
    """The looked-at proof: every size on dark and light plates, and a
    16px tab mockup. A glyph that dies at 16px is dead; this shows it."""
    from PIL import Image, ImageDraw
    im = Image.open(src).convert("RGBA")
    sizes = [256, 128, 64, 32, 16]
    W = sum(s for s in sizes) + 30 * len(sizes) + 20
    H = 2 * 256 + 20 + 40 + 30 + 2 * 90 + 20   # label, 2 plates, 2 tab mockups
    canvas = Image.new("RGBA", (W, H), (24, 26, 30, 255))
    dr = ImageDraw.Draw(canvas)
    dr.text((20, 8), f"{label}", fill=(230, 237, 243, 255))
    y_dark, y_light = 40, 40 + 256 + 20
    for plate, y in ((Image.new("RGBA", (W, 256), (22, 27, 34, 255)), y_dark),
                     (Image.new("RGBA", (W, 256), (246, 248, 250, 255)), y_light)):
        x = 20
        for s in sizes:
            plate.alpha_composite(im.resize((s, s), Image.LANCZOS), (x + (256 - s) // 2, (256 - s) // 2))
            x += s + 30
        canvas.alpha_composite(plate, (0, y))
    # browser-tab mockups at true tab scale (16px icon, 13px label)
    ty = y_light + 256 + 30
    for bg, tab, txt, name in (((15, 17, 21, 255), (22, 27, 34, 255), (201, 209, 217, 255), "dark tab"),
                               ((226, 232, 240, 255), (255, 255, 255, 255), (31, 41, 51, 255), "light tab")):
        dr.rounded_rectangle((20, ty, 260, ty + 34), 8, fill=tab)
        canvas.alpha_composite(im.resize((16, 16), Image.LANCZOS), (32, ty + 9))
        dr.text((56, ty + 9), f"HALO \u2014 {name}", fill=txt)
        ty += 90
    canvas.save(dst)
    print(f"sheet: {dst}")
    return dst


# ---------------------------------------------------------------- cli
def main():
    a = sys.argv
    if a[1] == "gen":
        kw = {}
        for i in range(4, len(a), 2):
            k, v = a[i].lstrip("-"), a[i + 1]
            kw[k] = int(v) if k in ("size", "seed") else v
        gen(a[2], Path(a[3]), **kw)
    elif a[1] == "key":
        key(Path(a[2]), Path(a[3]))
    elif a[1] == "shrink":
        shrink(Path(a[2]), Path(a[3]))
    elif a[1] == "ico":
        build_ico([Path(p) for p in a[2:-1]], Path(a[-1]))
    elif a[1] == "sheet":
        sheet(Path(a[2]), Path(a[3]), a[4] if len(a) > 4 else a[2])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
