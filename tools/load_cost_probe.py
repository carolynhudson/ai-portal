"""What does one model load actually cost, and what do llama.cpp's own defaults decide?

Three open questions this answers with loads rather than reasoning:

1. HEADROOM_BYTES is 4 GiB, added to weights+KV as "runtime overhead". §6.12.2 measured the real
   footprint running ~12 GiB above the file size on the 87 GB model. Since the host-RAM caches got
   priced separately, is 4 GiB of aperture headroom still right, too small, or partly double counting?
2. personality.AUTO_PARALLEL_SLOTS = 4 came from an earlier investigation. The server prints what it
   chose; read it instead of trusting the note.
3. --fit defaults to 'on' with a 1024 MiB target margin, so the server adjusts *unset* arguments to
   fit device memory. What does it actually do when ctx is given explicitly and n-gpu-layers is not?

Each load is measured, not sampled: GTT before, GTT after load, GTT after generation, RSS at each
point, and the log lines where the server states its own decisions. Models are picked to stay well
inside the ~53 GiB of GTT free beside the router that is already running.
"""
import json, re, signal, subprocess, sys, time, urllib.request

sys.path.insert(0, "/home/cortana/ai-projects/ai-portal")
from app.core import db, modelindex as mi

BIN = "/home/cortana/ai/releases/llama.cpp-b10964/llama-server"
PORT = 8127
GTT = "/sys/class/drm/card1/device/mem_info_gtt_used"
GiB = 2 ** 30


def gtt():
    try:
        return int(open(GTT).read().strip())
    except OSError:
        return 0


def rss(pid):
    try:
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except OSError:
        return 0


def probe(row, ctx):
    log_path = "/tmp/headroom_probe.log"
    open(log_path, "w").close()
    base = gtt()
    log = open(log_path, "wb")
    p = subprocess.Popen([BIN, "--model", row["path"], "--host", "127.0.0.1", "--port", str(PORT),
                          "-c", str(ctx), "-ngl", "99", "-v"],
                         stdout=log, stderr=subprocess.STDOUT)
    out = {"id": row["id"], "ctx": ctx, "gtt_base": base}
    try:
        for _ in range(240):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
                break
            except Exception:
                time.sleep(1)
        else:
            out["error"] = "no health"
            return out
        out["gtt_loaded"], out["rss_loaded"] = gtt(), rss(p.pid)
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/completion",
                                     data=json.dumps({"prompt": "Say OK.", "n_predict": 64,
                                                     "temperature": 0}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            r.read()
        time.sleep(2)
        out["gtt_after_gen"], out["rss_after_gen"] = gtt(), rss(p.pid)
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=40)
        except subprocess.TimeoutExpired:
            p.kill(); p.wait()
        log.close()
        time.sleep(4)
    out["gtt_after_exit"] = gtt()

    txt = open(log_path, errors="replace").read()
    def grab(pat):
        m = re.search(pat, txt)
        return m.group(0) if m else None
    out["parallel_line"] = grab(r"n_parallel = \d+") or grab(r"parallel = \d+")
    out["fit_lines"] = [l for l in txt.splitlines() if "fit" in l.lower() and "params" in l.lower()][:3]
    out["fit_set"] = [l.strip()[-90:] for l in txt.splitlines() if "fit:" in l][:6]
    out["kv_line"] = grab(r"KV cache[^\n]*") or grab(r"kv cache[^\n]*")
    out["ckpt_sizes"] = sorted({float(s) for _, s in
                               re.findall(r"n_tokens = (\d+), size = ([\d.]+) MiB", txt)})
    out["seq_rm_line"] = grab(r"seq rm[^\n]*") or grab(r"partial[^\n]*removal[^\n]*")
    return out


db.init(); mi.init()
rows = {r["id"]: r for r in mi.rows()}
CTX = 32768
targets = ["Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M", "Qwen3.6-35B-A3B-Q4_K_M",
           "ThinkingCap-Qwen3.6-27B-Q8_0"]

print(f"ctx {CTX} for every model; GTT base varies per run and is recorded per run\n")
print(f"{'model':<32} {'weights':>8} {'KV pred':>8} {'apex pred':>9} {'GTT delta':>9} "
      f"{'unexplained':>11} {'RSS peak':>9}")
for mid in targets:
    r = rows[mid]
    o = probe(r, CTX)
    if o.get("error"):
        print(f"  {mid}: {o['error']}"); continue
    pred_kv = mi.repriced_kv(r, CTX)
    pred_peak = mi.peak_bytes(r, CTX)
    measured = o["gtt_after_gen"] - o["gtt_base"]
    unexplained = measured - ((r["est_weights"] or 0) + (pred_kv or 0))
    print(f"{mid[:32]:<32} {(r['est_weights'] or 0)/GiB:>7.2f}G {(pred_kv or 0)/GiB:>7.2f}G "
          f"{(pred_peak or 0)/GiB:>8.2f}G {measured/GiB:>8.2f}G {unexplained/GiB:>10.2f}G "
          f"{max(o['rss_loaded'], o['rss_after_gen'])/GiB:>8.2f}G")
    print(f"    predicted headroom {mi.HEADROOM_BYTES/GiB:.2f} GiB | measured above weights+KV "
          f"{unexplained/GiB:.2f} GiB")
    print(f"    server said: parallel={o['parallel_line']!r} kv={o['kv_line']!r}")
    print(f"    fit lines: {o['fit_set'] or o['fit_lines']}")
    print(f"    checkpoint sizes (MiB): {o['ckpt_sizes']}")
    print(f"    GTT after exit: {o['gtt_after_exit']/GiB:.2f} GiB (base was {o['gtt_base']/GiB:.2f})")
    print()
