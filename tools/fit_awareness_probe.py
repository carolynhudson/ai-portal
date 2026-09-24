"""Does --fit act on measured free memory, or only report it?

Run A of the previous experiment proved the reporting half: with a router already holding 54.5 GiB,
fit logged "projected to use 38893 MiB of device memory vs. 54888 MiB of free device memory" and
"will leave 15995 >= 1024 MiB of free device memory, no changes needed". Free memory that correctly
accounts for another process is not blindness.

This is the acting half: reserve enough with a hog that the same load no longer fits, and read what
fit decides. The hog is smaller than before so the probe can actually come up (the previous attempt
never reached health and proved nothing).
"""
import re, signal, subprocess, sys, time, urllib.request

sys.path.insert(0, "/home/cortana/ai-projects/ai-portal")
from app.core import db, modelindex as mi

BIN = "/home/cortana/ai/releases/llama.cpp-b10964/llama-server"
GTT = "/sys/class/drm/card1/device/mem_info_gtt_used"
GiB = 2 ** 30
MID = "Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M"


def gtt():
    try:
        return int(open(GTT).read().strip())
    except OSError:
        return 0


def start(extra, log_path, model_path):
    log = open(log_path, "wb")
    return subprocess.Popen([BIN, "--model", model_path, "--host", "127.0.0.1", "-ngl", "99", "-v"]
                            + extra, stdout=log, stderr=subprocess.STDOUT), log


def wait(p, seconds):
    for _ in range(seconds):
        if p.poll() is not None:
            return "exited"
        try:
            urllib.request.urlopen("http://127.0.0.1:8132/health", timeout=2).read()
            return "ready"
        except Exception:
            time.sleep(1)
    return "timeout"


db.init(); mi.init()
row = next(r for r in mi.rows() if r["id"] == MID)
pred = (row["est_weights"] or 0) + (mi.repriced_kv(row, row["ctx_trained"]) or 0)
print(f"{MID}: weights+KV at trained ctx = {pred/GiB:.2f} GiB; fit-target default 1024 MiB")
print(f"GTT base {gtt()/GiB:.2f} GiB of 108.00, i.e. {108 - gtt()/GiB:.2f} GiB free\n")

print("--- hog: -c 393216 reserves ~12 GiB of KV")
hog, hoglog = start(["-c", "393216", "--port", "8133"], "/tmp/fit2_hog.log", row["path"])
hog_status = None
for _ in range(300):
    try:
        urllib.request.urlopen("http://127.0.0.1:8133/health", timeout=2).read()
        hog_status = "ready"
        break
    except Exception:
        time.sleep(1)
print(f"    hog {hog_status}; GTT now {gtt()/GiB:.2f} GiB, free {108 - gtt()/GiB:.2f} GiB")
print(f"    -> the probe needs {pred/GiB:.2f} GiB against {108 - gtt()/GiB:.2f} GiB free\n")

print("--- probe: identical settings to run A, with --fit on and no -c")
p, log = start(["--fit", "on", "--port", "8132"], "/tmp/fit2_probe.log", row["path"])
status = wait(p, 600)
print(f"    probe status: {status}")
time.sleep(2)
measured = gtt()
p.send_signal(signal.SIGTERM)
try:
    p.wait(timeout=40)
except subprocess.TimeoutExpired:
    p.kill(); p.wait()
log.close()
hog.send_signal(signal.SIGTERM)
try:
    hog.wait(timeout=40)
except subprocess.TimeoutExpired:
    hog.kill(); hog.wait()
hoglog.close()
time.sleep(4)

txt = open("/tmp/fit2_probe.log", errors="replace").read()
print("\nwhat fit decided:")
for line in [l for l in txt.splitlines() if "fit" in l.lower() or "n_ctx " in l or "no changes" in l][:12]:
    print("    " + line.split(" I ")[-1].strip())
print(f"\nGTT after probe: {measured/GiB:.2f} GiB; after everything exited: {gtt()/GiB:.2f} GiB")
