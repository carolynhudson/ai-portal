#!/usr/bin/env python3
"""Issue a mesh identity (docs/MESH_PROPOSAL.md, phase P0).

    python3 tools/issue_agent_token.py rainbowblock --machine RainbowBlock.local

Prints the token **once**, with a paste-ready `mcp` block for the peer's
opencode.json. The portal stores the token's hash and can never show it
again - losing it means issuing a new identity, which is the point.

Scopes: `read` (the KB's reading tools - the P0 default) or `read,write`
(adds memory_add). Destructive tools (memory_remove) are never on a token.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import agents  # noqa: E402

URL = "http://192.168.1.228:8090/mcp"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="issue a mesh agent token")
    ap.add_argument("name", help="the identity's slug, e.g. rainbowblock-forge")
    ap.add_argument("--machine", default="", help="which box this identity runs on")
    ap.add_argument("--scopes", default="read", help="read | read,write")
    args = ap.parse_args(argv)

    scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
    try:
        row, token = agents.issue(args.name, args.machine, scopes)
    except agents.AgentsError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"# identity {row['name']} on {row['machine'] or '-'} scopes={','.join(row['scopes'])}")
    print(f"# token shown ONCE - store it in the peer's config now; revoke and re-issue to rotate")
    print(token)
    print()
    print("# paste into the peer's opencode.json (portal Settings -> Agents lists the identity):")
    print('#   "mcp": {')
    print('#     "halo-kb": {')
    print(f'#       "type": "remote",')
    print(f'#       "url": "{URL}",')
    print(f'#       "headers": {{"Authorization": "Bearer {token}"}}')
    print("#     }")
    print("#   }")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
