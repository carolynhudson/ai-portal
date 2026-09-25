"""Does per-model `load-on-startup` in a preset section decide what loads at startup?

The first attempt proved nothing because it read fields that are not there -- /models returned
entries whose `name` and `loaded` keys came back None, so the assertion was vacuous. This prints the
payload shape first and then asks the question against whatever the router actually reports.

Question that matters for the portal: can a personality decide which models autoload, from
presets.ini alone, or does switching personalities also require editing the unit's command line?
"""
import json, signal, subprocess, time, urllib.request

BIN = "/home/cortana/ai/releases/llama.cpp-b10964/llama-server"
MODELS = "/tmp/probe_models"
PORT = 8141
INI = "/tmp/lso_probe.ini"
LOG = "/tmp/lso_probe.log"
SMALL = "Qwen3.5-0.8B.Q4_K_M"


def run(ini_text, extra=(), seconds=120, label=""):
    open(INI, "w").write(ini_text)
    open(LOG, "w").close()
    cmd = [BIN, "--host", "127.0.0.1", "--port", str(PORT), "--models-dir", MODELS,
           "--models-preset", INI] + list(extra)
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
    time.sleep(3)          # startup loading is not instantaneous
    models, props = None, None
    if ready:
        for path, var in (("/models", "models"), ("/props", "props")):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=15) as r:
                    body = json.loads(r.read().decode())
                if var == "models":
                    models = body
                else:
                    props = {"total_slots": body.get("total_slots"),
                             "total_context": body.get("total_context")}
            except Exception as e:
                print(f"    {path} -> {e}")
    resident = []
    try:
        for line in open(f"/proc/{p.pid}/status"):
            if line.startswith("VmRSS:"):
                resident.append(int(int(line.split()[1]) / 2 ** 20))
    except OSError:
        pass
    children = subprocess.run(["pgrep", "-P", str(p.pid)], capture_output=True, text=True).stdout.split()
    child_rss = []
    for c in children:
        try:
            for line in open(f"/proc/{c}/status"):
                if line.startswith("VmRSS:"):
                    child_rss.append(int(int(line.split()[1]) / 2 ** 20))
        except OSError:
            pass
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=30)
    except subprocess.TimeoutExpired:
        p.kill(); p.wait()
    log.close()
    time.sleep(2)
    text = open(LOG, errors="replace").read()
    print(f"  [{label}] ready={ready} parent RSS={resident} child RSS={child_rss}")
    if models:
        data = models.get("data") or []
        print(f"  [{label}] /models has {len(data)} entries; keys: {sorted(data[0].keys()) if data else '[]'}")
        for d in data:
            print(f"      {json.dumps({k: v for k, v in d.items() if k in ('name','model','path','loaded','state')})}")
    if props:
        print(f"  [{label}] /props: {props}")
    interesting = [l.strip()[-95:] for l in text.splitlines()
                   if any(w in l.lower() for w in ("load", "autoload", "preset", "startup"))][:6]
    for l in interesting:
        print(f"      log: {l}")
    return models


print("=== A: global autoload OFF, the model's own section asks for load-on-startup = on")
run(f"[*]\nstop-timeout = 15\n\n[{SMALL}]\nload-on-startup = on\n",
    extra=["--no-models-autoload"], label="A")

print("\n=== B: identical file with the key OFF, global autoload left at its default (on)")
run(f"[*]\nstop-timeout = 15\n\n[{SMALL}]\nload-on-startup = off\n", label="B")

print("\n=== C: the key in the GLOBAL section instead -- does it apply to every model?")
run(f"[*]\nstop-timeout = 15\nload-on-startup = on\n", extra=["--no-models-autoload"], label="C")
