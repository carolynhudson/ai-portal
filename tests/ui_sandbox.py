"""Sandboxed portal instance for the UI tests.

Nothing in the UI test path may touch this machine's ~/ai, its systemd units,
or the network, so the sandbox is:

  - ``AI_PORTAL_HOME`` in a throwaway directory (the portal derives its whole
    ``~/ai`` tree from it, config and state included)
  - ``tests/ui/bin`` first on PATH: ``systemctl`` answers with a fixed unit
    state and logs every invocation instead of touching the real units;
    ``journalctl`` returns a fixed log tail; ``needs-restarting`` says "no"
  - a dead outbound proxy, so PyPI/HF probes fail fast into their documented
    offline states rather than depending on connectivity
  - the browser harness intercepting service/update mutations (see lib.mjs)

Run it by hand to debug a UI test failure:

    uv run python -m tests.ui_sandbox dashboard          # drive one page, show the report
    uv run python -m tests.ui_sandbox --serve            # leave it up and print the URL
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
UIDIR = ROOT / "tests" / "ui"
PASSWORD = "ui-test-password-123"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Sandbox:
    """A real uvicorn process serving the portal against a throwaway home."""

    def __init__(self, seed_content: bool = True):
        self.home = Path(tempfile.mkdtemp(prefix="aipp-ui-home-"))
        self.syscalls = self.home / "syscalls.log"
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.cookie = ""
        self.proc: subprocess.Popen | None = None
        self._log = None
        if seed_content:
            self._seed()

    def _seed(self) -> None:
        """A little fake content so pages render populated states."""
        cache = self.home / ".cache" / "huggingface" / "hub" / "models--x--y"
        cache.mkdir(parents=True)
        (cache / "blob").write_bytes(b"x" * 1024)
        mdir = self.home / "ai" / "models" / "llama" / "ui-test-model"
        mdir.mkdir(parents=True)
        (mdir / "ui-test-model-Q4_K_M.gguf").write_bytes(b"\0" * 2048)
        comfy = self.home / "ai" / "comfyui" / "models" / "checkpoints"
        comfy.mkdir(parents=True)
        (comfy / "ui-test.safetensors").write_bytes(b"\0" * 2048)
        self._seed_health()

    def _seed_health(self) -> None:
        """A machine the health pane can answer about deterministically.

        Version probes read real files (dist-info directories, a ROCm tree,
        presets.ini, the model index); the commands that would read the host's
        package database are shimmed in tests/ui/bin. Torch is seeded built for
        the same ROCm series the dpkg-query shim reports, so the version-coupling
        advisory stays quiet and the pane's attention list is the one thing the
        apt shim makes pending.
        """
        rocm = self.home / "rocm"
        (rocm / "core-9.9" / "lib").mkdir(parents=True)
        (rocm / "core").mkdir(parents=True)
        site = (self.home / "ai" / "comfyui" / "venv" / "lib"
                / "python3.12" / "site-packages")
        site.mkdir(parents=True)
        (site / "torch-2.9.0+rocm9.9.0.dist-info").mkdir()
        (self.home / "ai" / "comfyui" / "venv" / "pyvenv.cfg").write_text(
            "home = /usr/bin\nversion = 3.12.0\n")
        (self.home / "ai" / "models" / "llama" / "INDEX.json").write_text(json.dumps(
            {"index_version": "5.0", "scan": {"files_on_disk": 1, "total_bytes": 2048}}))
        presets = self.home / "ai" / "config" / "llama"
        presets.mkdir(parents=True)
        (presets / "presets.ini").write_text("[*]\nthreads = 16\n\n")
        (self.home / "apt-stamp").touch()   # apt's "last update" stamp: fresh lists

    def start(self, timeout: float = 60.0) -> "Sandbox":
        env = dict(os.environ)
        env.update(
            AI_PORTAL_HOME=str(self.home),
            AI_PORTAL_HOST="127.0.0.1",
            AI_PORTAL_PORT=str(self.port),
            AI_PORTAL_PASSWORD=PASSWORD,
            AI_PORTAL_TEST_SYSCALL_LOG=str(self.syscalls),
            # the health pane probes these; pointed into the sandbox so it reads
            # a fixed ROCm tree and a fixed apt stamp instead of this machine's
            AI_PORTAL_ROCM_ROOT=str(self.home / "rocm"),
            AI_PORTAL_APT_STAMP=str(self.home / "apt-stamp"),
            PATH=f"{UIDIR / 'bin'}{os.pathsep}{env['PATH']}",
            http_proxy="http://127.0.0.1:1",
            https_proxy="http://127.0.0.1:1",
            HTTP_PROXY="http://127.0.0.1:1",
            HTTPS_PROXY="http://127.0.0.1:1",
            no_proxy="127.0.0.1,localhost",
            NO_PROXY="127.0.0.1,localhost",
        )
        self._log = open(self.home / "server.log", "w+")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--no-access-log"],
            cwd=str(ROOT), env=env, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                self._log.seek(0)
                raise RuntimeError(f"sandbox exited ({self.proc.returncode}):\n{self._log.read()[-3000:]}")
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1).status_code == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            raise RuntimeError("sandbox never answered /healthz")
        r = httpx.post(f"{self.base}/api/login", json={"password": PASSWORD}, timeout=10)
        r.raise_for_status()
        token = r.cookies.get("aipp")
        if not token:
            raise RuntimeError("login did not set the session cookie")
        self.cookie = f"aipp={token}"
        return self

    def page(self, name: str, spec: str | Path = UIDIR / "spec.json",
             timeout: int = 300, stream: bool = False, **kw) -> dict:
        """Drive one page in jsdom and return the harness report.

        ``stream=True`` lets the harness talk to the terminal as it goes
        (UI_DEBUG=1 prints a timing line per sweep) — for debugging a page that
        is slow or stuck, at the cost of not capturing stderr.
        """
        cmd = [kw.pop("node", "node"), str(UIDIR / "run.mjs"),
               "--base", self.base, "--spec", str(spec), "--page", name,
               "--cookie", self.cookie]
        if stream:
            captured = subprocess.run(cmd, cwd=str(ROOT), text=True,
                                      timeout=timeout, stdout=subprocess.PIPE, **kw)
            raw = captured.stdout.strip()
            report = json.loads(raw) if raw else {"ok": False, "failures": ["no output"]}
            report["_stderr"] = "(streamed)"
            return report
        out = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                             timeout=timeout, **kw)
        raw = out.stdout.strip()
        if not raw:
            raise RuntimeError(f"no harness output for {name}\ncmd: {' '.join(cmd)}\n"
                               f"stderr:\n{out.stderr[-3000:]}")
        report = json.loads(raw)
        report["_stderr"] = out.stderr[-3000:]
        return report

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log:
            self._log.close()

    def server_log(self) -> str:
        if self._log:
            self._log.seek(0)
            return self._log.read()
        return ""

    def __enter__(self) -> "Sandbox":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def main(argv: list[str]) -> int:
    pages = [p["name"] for p in json.loads((UIDIR / "spec.json").read_text())["pages"]]
    if argv and argv[0] == "--serve":
        sbx = Sandbox().start()
        print(f"sandbox portal: {sbx.base}")
        print(f"cookie:  {sbx.cookie}")
        print(f"home:    {sbx.home}")
        print(f"syscalls:{sbx.syscalls}")
        print("Ctrl-C to stop")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            sbx.stop()
        return 0
    for name in argv or pages:
        with Sandbox() as sbx:
            report = sbx.page(name)
            print(json.dumps(report, indent=2))
            print(f"--- {name}: {'OK' if report['ok'] else 'FAILURES'} ---", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
