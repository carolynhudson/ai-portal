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
import json, os, pathlib, re, shutil, signal, subprocess, sys, tempfile, time, urllib.request

# The build the box actually runs: a key's preset eligibility is per-build, so asking an old
# binary about the file the new router will read measures the wrong thing. Override with
# --binary / $LLAMA_BIN; otherwise newest installed release, then the router's own env pointer.
def newest_release_server(argv: list[str]) -> str:
    """The llama-server whose behaviour the written file will be read by."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    pinned = os.environ.get("LLAMA_BIN", "")
    if "--binary" in argv:
        i = argv.index("--binary")
        if i + 1 >= len(argv):
            raise SystemExit("--binary needs a path")
        pinned = argv[i + 1]
    if not pinned:
        try:
            from app.core import serviceconf
            pinned = serviceconf.read_service_env("llamacpp").get("LLAMA_BIN", "")
        except Exception:
            pinned = ""
    releases = sorted((p for p in pathlib.Path("/home/cortana/ai/releases").glob("llama.cpp-b*")
                       if re.fullmatch(r"llama\.cpp-b\d+", p.name)),
                      key=lambda p: int(p.name.removeprefix("llama.cpp-b")), reverse=True)
    for cand in (pinned, *(str(p / "llama-server") for p in releases)):
        if cand and pathlib.Path(cand).is_file():
            return str(cand)
    raise SystemExit("no llama-server found — pass --binary <path>")


BIN = newest_release_server(sys.argv)


# Deliberately a probe directory holding ONE small model: with autoload left on, --models-dir is
# the blast radius, and pointing it at the real library would have autoloaded 16 models beside a
# router already holding 54.5 GiB.
# Private scratch dir, not fixed /tmp names: this file writes a preset the router reads, and a
# world-writable path that a router will parse is a place another user can put a preset in.
PROBE_DIR = tempfile.mkdtemp(prefix="ai-portal-eligibility-")
os.chmod(PROBE_DIR, 0o700)
MODELS = os.path.join(PROBE_DIR, "models")
# The fixture model this box has: a probe directory is built from it on demand, because the tiny
# library model this probe used to symlink no longer exists and a probe that silently has no
# fixture reports "accepted" for every key.
FIXTURE_SRC = ("/home/cortana/.cache/huggingface/hub/models--ggml-org--embeddinggemma-300M-GGUF"
               "/snapshots")
PORT = 8140
INI = os.path.join(PROBE_DIR, "eligibility_probe.ini")
LOG = os.path.join(PROBE_DIR, "eligibility_probe.log")
SMALL = "embeddinggemma-300M"

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
    # The per-model sampling settings a model's upstream "recommended llama settings" are made
    # of, plus the two context levers b11396 added. Asked because the portal's registry (20 keys)
    # cannot express any of them, so a personality cannot record a model's recommended settings.
    ("temp", "1.0"), ("top-k", "20"), ("top-p", "0.95"), ("min-p", "0.0"),
    ("presence-penalty", "0.0"), ("repeat-penalty", "1.0"), ("dry-multiplier", "1.0"),
    ("kv-unified-per-slot", "8192"), ("fit-ctx", "8192"),
]


def ensure_fixture() -> str:
    """Build the probe's one-model directory from a model this box actually has.

    A probe with an empty --models-dir reports 'accepted' for every key, because the router
    loads nothing and never reads the file. So the fixture is created here, and its absence
    is returned as a hard failure rather than a clean sheet.
    """
    src = sorted(pathlib.Path(FIXTURE_SRC).glob("*/embeddinggemma-300M-Q8_0.gguf"))
    if not src:
        raise SystemExit(f"no fixture model under {FIXTURE_SRC} — the probe cannot measure anything")
    target = pathlib.Path(MODELS) / SMALL
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot build probe fixture at {target}: {e}") from e
    (target / src[0].name).symlink_to(src[0])
    return str(src[0])


def run(ini_text, extra=(), seconds=25):
    pathlib.Path(INI).write_text(ini_text, encoding="utf-8")
    cmd = [BIN, "--host", "127.0.0.1", "--port", str(PORT), "--models-dir", MODELS,
           "--models-preset", INI, "-v"] + list(extra)
    p = None
    try:
        with open(LOG, "wb") as log:
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
                p.kill()
                p.wait()
    except OSError as e:
        if p is not None:
            p.kill()
        return False, None, f"<probe log {LOG} unusable: {e}>"
    time.sleep(2)
    try:
        text = pathlib.Path(LOG).read_text(errors="replace")
    except OSError as e:
        text = f"<log unreadable: {e}>"
    return ready, payload, text


def classify(text):
    m = re.search(r"option '([^']+)' not recognized in preset", text)
    if m:
        return "key rejected", m.group(0)
    m = re.search(r"(invalid|unknown|failed to parse)[^\n]{0,90}", text, re.I)
    if m:
        return "value problem", m.group(0)[:90]
    return "accepted", ""


fixture = ensure_fixture()
print(f"build: {BIN.split('/')[-1]}   fixture: {SMALL} <- {fixture}")
print(f"scratch: {PROBE_DIR}")
print("positive control: 'mmap' must be rejected\n")
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
autoload_lines = [ln.strip()[-70:] for ln in text.splitlines() if "autoload" in ln.lower()]
print(f"    fit/autoload lines: {autoload_lines[:3]}")

print("\n--- B: autoload ON globally (the default), same model asks for load-on-startup = off")
ready2, payload2, text2 = run(f"[*]\nstop-timeout = 15\n\n[{SMALL}]\nload-on-startup = off\n",
                              seconds=90)
loaded2 = [{d.get("name"): d.get("loaded")} for d in (payload2 or {}).get("data", [])] if payload2 else None
print(f"    ready={ready2}  loaded flags: {loaded2}")
