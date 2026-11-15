"""Mesh hub client (P0).

The portal's route to the mirrored, ciphertext-only secrets broker on
the Pi pair (docs/MESH_HUB_DESIGN.md). meshctl is the crypto agent:
age encryption/decryption happen client-side inside meshctl; the hub
never holds plaintext. Rules inherited from NODE_CONTRACT: secrets
ride env/broker, never config files; never log secret bodies.

All calls are blocking (subprocess): web handlers run them via
asyncio.to_thread. Observations are free; mutations (put/rm/share)
belong behind the portal's confirm gate at the route layer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


class MeshHubError(RuntimeError):
    pass


def _bin() -> str:
    return (
        os.environ.get("AIPP_MESHCTL")
        or shutil.which("meshctl")
        or str(REPO / "mesh" / "hub" / "target" / "release" / "meshctl")
    )


def _run(args: list[str], timeout: int = 45, binary: bool = False) -> bytes:
    exe = _bin()
    if not os.access(exe, os.X_OK):
        raise MeshHubError("meshctl not installed")
    try:
        p = subprocess.run([exe] + args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise MeshHubError(f"meshctl {args[0]}: timeout") from exc
    if p.returncode != 0:
        # meshctl writes its own redacted errors to stderr; no payloads.
        raise MeshHubError(p.stderr.decode("utf-8", "replace").strip()[:300])
    return p.stdout if binary else p.stdout


def available() -> bool:
    """True when the local mesh identity exists and a hub answers.

    Probes with a real authenticated call: the hub requires a
    mesh-CA client certificate on every TLS connection by design.
    """
    try:
        nodes()
        return True
    except (MeshHubError, ValueError, KeyError):
        return False


def nodes() -> list[dict]:
    data = json.loads(_run(["nodesjson"]))
    return data.get("nodes", [])


def listing() -> list[dict]:
    data = json.loads(_run(["lsjson"]))
    return [
        {
            "name": r["name"],
            "kind": r.get("kind", "secret"),
            "size": r.get("size", 0),
            "recipients": r.get("recipients", []),
            "updated": r.get("updated", 0),
        }
        for r in data.get("secrets", [])
    ]


def get(name: str) -> bytes:
    return _run(["get", name], binary=True)


def get_text(name: str) -> str:
    return get(name).decode("utf-8", "replace")


def put(name: str, data: bytes, recipients: list[str] | None = None,
        kind: str = "secret") -> None:
    """Store a secret (caller supplies plaintext; meshctl encrypts).

    Mutation: route layer must confirm-gate this.
    """
    fd, tmp = tempfile.mkstemp(prefix="meshput-")
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.close(fd)
        os.chmod(tmp, 0o600)
        args = ["put", name, "--from", tmp]
        if recipients:
            args += ["--to", ",".join(recipients)]
        if kind == "blob":
            args.append("--blob")
        _run(args)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def share(name: str, recipients: list[str]) -> None:
    """Owner re-designation (client-side re-encrypt). Mutation."""
    _run(["share", name, "--to", ",".join(recipients)])


def rm(name: str) -> None:
    """Mutation: confirm-gate at the route layer."""
    _run(["rm", name])
