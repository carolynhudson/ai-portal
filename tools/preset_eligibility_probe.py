"""Which keys can presets.ini actually carry, measured against the binary on this box.

The portal writes presets.ini and the router reads it at load time without explaining itself. A key
the router does not accept is not ignored: `mmap` in `[*]` makes it reject the whole file with
"option 'mmap' not recognized in preset '*'" -- the failure that took the managed router down. That
is only knowable per key, per build, so it is measured rather than assumed, with `mmap` as the
positive control that proves the probe can detect rejection.

Two outcomes are kept apart on purpose:
  * the KEY is not a preset option at all  -> the file is rejected;
  * the KEY is accepted but the VALUE is not -> different message, different problem.

Second half: does per-model `load-on-startup` in a preset section control what loads at startup,
without touching the unit's command line? If it does, a personality can decide startup loading from
the preset file alone.
"""
import json, re, signal, subprocess, sys, time, urllib.request

BIN = "/home/cortana/ai/releases/llama.cpp-b10964/llama-server"
# Deliberately a probe directory holding ONE small model: with autoload left on, --models-dir is
# the blast radius, and pointing it at the real library would have autoloaded 16 models beside a
# router already holding 54.5 GiB.
MODELS = "/tmp/probe_models"
PORT = 8140
INI = "/tmp/eligibility_probe.ini"
LOG = "/tmp/eligibility_probe.log"
SMALL = "Qwen3.5-0.8B.Q4_K_M"

CANDIDATES = [
    ("threads", "16"), ("lazy-mode", "on"), ("stop-timeout", "15"), ("ctx-size", "4096"),
    ("parallel", "4"), ("flash-attn", "on"), ("n-gpu-layers", "99"), ("fit", "on"),
    ("fit-target", "1024"), ("keep", "0"), ("cache-ram", "8192"), ("ctx-checkpoints", "32"),
    ("checkpoint-min-step", "8192"), ("cache-idle-slots", "on"), ("jinja", "on"),
    ("reasoning-budget", "1024"), ("reasoning-effort", "low"),
    ("dedup-cache-models", "on"), ("load-on-startup", "on"),
    ("models-max", "1"), ("models-autoload", "off"),
    ("api-key", "probe-secret"), ("no-webui", "on"), ("metrics", "on"),
    ("override-kv", "probe.int=1"), ("mmproj", "/nonexistent.gguf"),
    ("alias", "probe"), ("model", "/nonexistent.gguf"),
    ("mmap", "on"),            # positive control: known to be rejected
]


def run(ini_text, extra=(), seconds=25):
    open(INI, "w").write(ini_text)
    open(LOG, "w").close()
    cmd = [BIN, "--host", "127.0.0.1", "--port", str(PORT), "--models-dir", MODELS,
           "--models-preset", INI, "-v"] + list(extra)
    log = open(LOG, "wb")
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
    ready = False
    for _ in range(seconds):
        if p.poll() is not None:
            break
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()
            ready = True
            break
        except Exception:
            time.sleep(1)
    payload = None
    if ready:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/models", timeout=10) as r:
                payload = json.loads(r.read().decode())
        except Exception:
            payload = None
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
    log.close()
    time.sleep(2)
    return ready, payload, open(LOG, errors="replace").read()


def classify(text):
    m = re.search(r"option '([^']+)' not recognized in preset", text)
    if m:
        return "key rejected", m.group(0)
    m = re.search(r"(invalid|unknown|failed to parse)[^\n]{0,90}", text, re.I)
    if m:
        return "value problem", m.group(0)[:90]
    return "accepted", ""


print(f"build: {BIN.split('/')[-1]}   positive control: 'mmap' must be rejected\n")
print(f"{'key':<22} {'verdict':<14} evidence")
elig, rejected = {}, {}
for key, val in CANDIDATES:
    ready, _, text = run(f"[*]\n{key} = {val}\n")
    verdict, evidence = classify(text)
    if verdict == "accepted" and not ready:
        verdict, evidence = "started but unhealthy", ""
    elig[key] = verdict
    if verdict == "key rejected":
        rejected[key] = evidence
    print(f"{key:<22} {verdict:<14} {evidence[:78]}")

print(f"\ncontrol holds: mmap rejected = {elig.get('mmap') == 'key rejected'}")
print(f"keys the router will not accept in a preset: {sorted(rejected)}")

print("\n=== load-on-startup: does a preset section decide what loads at startup?")
print("--- A: autoload OFF globally, one model asks for load-on-startup = on")
ready, payload, text = run(f"[*]\nstop-timeout = 15\n\n[{SMALL}]\nload-on-startup = on\n",
                           extra=["--no-models-autoload"], seconds=90)
loaded = [{d.get("name"): d.get("loaded")} for d in (payload or {}).get("data", [])] if payload else None
print(f"    ready={ready}  loaded flags: {loaded}")
print(f"    fit/autoload lines: {[l.strip()[-70:] for l in text.splitlines() if 'autoload' in l.lower()][:3]}")

print("\n--- B: autoload ON globally (the default), same model asks for load-on-startup = off")
ready2, payload2, text2 = run(f"[*]\nstop-timeout = 15\n\n[{SMALL}]\nload-on-startup = off\n",
                              seconds=90)
loaded2 = [{d.get("name"): d.get("loaded")} for d in (payload2 or {}).get("data", [])] if payload2 else None
print(f"    ready={ready2}  loaded flags: {loaded2}")
