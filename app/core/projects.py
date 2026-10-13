"""Projects: the registry, the folder scan, and the shortlist of ways to start one (§6.14.1n).

The operator's shape for the Projects page has two states. In the inactive state this module is the
whole page: projects are created as sub-folders of the projects root, existing folders are adopted,
and the small selection of setup tasks runs from the boxes the operator ticked. The harness launch
belongs to the operational state (queue 18c) and is not here.

What this module owns
---------------------
**The name rule.** A project name becomes a directory under the root. A name containing `/`, `..`,
a leading `-`, or a NUL is not a name, and a create that "worked" on one of those is a folder the
portal can never show the operator. So the rule is stated (lowercase letters, digits and single
hyphens, 1-64 characters, matching the directory name), and refusals name the rule.

**Toolchain honesty.** The page must not offer a dead button. `toolchains()` measures what is on
this box right now (`shutil.which` plus the tool's own version line), `plan_init()` splits the
requested steps into *what will run* and *what is refused, with the install command*, and `create()`
only executes the first set. On this box that distinction is real: uv, git, node, npm, g++, cmake
and make are present; bun, deno, pnpm, yarn, cargo, rustc, go, meson, conan, dotnet, java and zig
are not. A shortlist that offered Rust with a live button would fail with a subprocess error the
operator has to decode; here it says "rust is not installed — apt install rustc cargo".

**Bounded commands.** Every init command runs with a timeout, a list argv (never a shell), cwd
pinned to the new project, `stdin` closed, and stdout/stderr captured. A timeout kills the child
group. Nothing is piped, nothing is evaluated.

**Honest partial creation.** If step 3 of 5 fails, the folder stays, the files written by steps 1-2
stay, and the answer names the step that failed, the command, and the exit code. The portal does
not silently delete a half-built project: a folder that exists and a registry row that does not is
a fact the page can show; a folder that was created and then quietly removed is not.

**Registry plus scan, never registry instead of scan.** The registry stores what the portal created
and what the operator adopted. A folder appearing in the projects root (created by hand, by pi, by
`git clone`) is discovered by `scan()`, and adoption is an explicit verb with a name check. A
registry that is the only truth is a registry that loses projects the moment someone uses `mkdir`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from . import db, paths, portalsettings

#: The tools an init step may need. Names are the binaries, not the ecosystems.
TOOLS = ("git", "uv", "python3", "node", "npm", "g++", "cmake", "make", "cargo", "rustc", "go",
         "bun", "deno", "pnpm", "yarn", "meson", "conan")

#: What to tell the operator when a tool is missing. Only for toolchains a project plausibly wants;
#: an empty statement for a tool nobody asked about.
INSTALL_HINT = {
    "cargo": "rust is not installed — apt install rustc cargo (or: curl https://sh.rustup.rs -sSf | sh)",
    "rustc": "rust is not installed — apt install rustc cargo (or: curl https://sh.rustup.rs -sSf | sh)",
    "go": "go is not installed — apt install golang-go",
    "bun": "bun is not installed — curl -fsSL https://bun.sh/install | bash",
    "deno": "deno is not installed — curl -fsSL https://deno.land/install.sh | sh",
    "pnpm": "pnpm is not installed — npm install -g pnpm",
    "yarn": "yarn is not installed — npm install -g yarn",
    "cmake": "cmake is not installed — apt install cmake",
    "uv": "uv is not installed — apt install uv, or: curl -LsSf https://astral.sh/uv/install.sh | sh",
}

#: Letters and digits, with at most one hyphen between them: `app--1` is a typo of a name, not a
#: name, and a separator, a leading dot, a space or a NUL is the shape a path traversal arrives as.
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_MAX_NAME = 64

#: A remote is handed to git. `ext::` is git's own "run this command to talk to the remote"
#: transport, so it is refused by name, not by scheme list; a password in the URL is refused
#: because git writes it into `.git/config` in the clear, where the project's own files can read it.
_REMOTE_MAX = 500
_REMOTE_SCHEMES = ("https://", "http://", "ssh://", "git+ssh://", "git://", "file://")
_SCP_LIKE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^ \t\n]+$")
_TEXT_MAX = 20000
_TEXT_FILES = {"readme": "README.md", "agents": "AGENTS.md"}

# The operator's rule for .gitignore: it is not a choice, it is a consequence of the choices.
# Checking "git repository" should produce the file, filled in from the stacks that were picked.
_GITIGNORE_BASE = (".DS_Store", "*.log", ".env", ".env.local", ".scratch/")
_GITIGNORE_STACKS = {
    "python": ("__pycache__/", "*.py[oc]", ".venv/", "*.egg-info/"),
    "node": ("node_modules/", "dist/", "*.tsbuildinfo", ".npm/"),
    "cpp": ("build/", "cmake-build-*/", "CMakeCache.txt", "compile_commands.json", "*.o", "*.so"),
}

#: A file the init would write, and a file that arrived with the remote, is a decision — not a
#: merge conflict to resolve in code. The three answers are the ones the operator asked for.
DECISIONS = ("replace", "keep", "append")
_STAGING_MAX_AGE_S = 30 * 60
_PLAN_ID = re.compile(r"^[0-9a-f]{32}$")

#: The shortlist. Deliberately small: the common best practices for starting a coding project, and
#: the details left to the agent inside the session, as the operator asked.
INIT_STEPS: dict[str, dict[str, Any]] = {
    "git": {
        "label": "Git repository",
        "needs": ["git"],
        "does": "git init. The first commit is left to you or to the agent: it needs an identity, "
                "and a portal that sets a fake git identity on your machine is overstepping",
        "commands": [["git", "init", "-q", "."]],
    },
    "python": {
        "label": "Python (uv virtualenv)",
        "needs": ["uv"],
        "does": "uv venv, and a requirements.txt for the project to grow into",
        "commands": [["uv", "venv", "-q"], ],
        "files": {"requirements.txt": ""},
    },
    "node": {
        "label": "Node / TypeScript",
        "needs": ["node", "npm"],
        "does": "npm init -y, a tsconfig.json, and the files the folder needs to be buildable",
        "commands": [["npm", "init", "-y"]],
        "files": {"tsconfig.json": json.dumps({
            "compilerOptions": {"strict": True, "target": "ES2022", "module": "NodeNext",
                                "outDir": "dist", "rootDir": "src"},
            "include": ["src"]}, indent=2) + "\n"},
        "dirs": ["src"],
    },
    "cpp": {
        "label": "C++ (CMake)",
        "needs": ["cmake", "g++"],
        "does": "CMakeLists.txt and a build/ directory",
        "commands": [],
        "files": {"CMakeLists.txt": "cmake_minimum_required(VERSION 3.25)\nproject(${name} CXX)\n\n"
                                    "file(GLOB_RECURSE SOURCES CONFIGURE_DEPENDS src/*.cpp)\n"
                                    "add_executable(${name} ${SOURCES})\n"},
        "dirs": ["src", "build"],
    },
    "readme": {
        "label": "README.md",
        "needs": [],
        "does": "a README with the project name and an empty body to fill in",
        "commands": [],
        "files": {"README.md": "# ${name}\n\nWhat this project is, and how to run it.\n"},
    },
    "agents": {
        "label": "AGENTS.md",
        "needs": [],
        "does": "the file a coding agent reads on entry: the project's commands and boundaries",
        "commands": [],
        "files": {"AGENTS.md": "# ${name}\n\n"
                               "## Commands\n"
                               "- test: (fill in)\n"
                               "- lint: (fill in)\n"
                               "- run: (fill in)\n\n"
                               "## Boundaries\n"
                               "- stay inside this folder\n"
                               "- do not rewrite files outside it without saying why\n"},
    },
}

#: Order matters: files before the commit, toolchain steps before the files they sit inside.
INIT_ORDER = ("git", "python", "node", "cpp", "readme", "agents")


class ProjectError(Exception):
    """A refusal with a sentence attached. The endpoint turns it into a 409, the page shows it.

    `conflicts` carries the files the operator has to choose about (see `DECISIONS`) when a refusal
    is "this needs an answer", so the page can ask instead of guessing.
    """

    def __init__(self, message: str, conflicts: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.conflicts = conflicts or []


def compose_gitignore(steps: list[str]) -> str:
    """The .gitignore for these choices: the base, then one labelled section per stack picked.

    Deliberately not a checkbox. A git repository with no ignores is a repository that will commit
    its own `.venv`, and asking the operator to remember that is asking them to do the portal's job.
    """
    lines = list(_GITIGNORE_BASE)
    for step in INIT_ORDER:
        if step in steps and step in _GITIGNORE_STACKS:
            lines.append("")
            lines.append(f"# {INIT_STEPS[step]['label']}")
            lines.extend(_GITIGNORE_STACKS[step])
    return "\n".join(lines) + "\n"


def init_of(path: str) -> list[str]:
    """The init steps the registry recorded for this folder, in init order.

    The ignores are composed from the steps, so a folder that arrived by adoption has to answer the
    same question a freshly created one does. A folder the registry does not know answers with
    nothing — this page does not guess what a folder contains.
    """
    row = db.query("SELECT notes FROM projects WHERE path = ?", (os.path.realpath(path),))
    if not row:
        return []
    try:
        notes = json.loads(row[0]["notes"] or "{}")
    except (TypeError, ValueError):
        return []
    ran = notes.get("init") or []
    return [step for step in INIT_ORDER if isinstance(ran, list) and step in ran]


def _write_gitignore(target: str, ran: list[str], log: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Write the derived ignores. Returns a failed-step record only if the write itself fails.

    Not a step: `plan["run"]` is what the operator ticked, and the file is what those ticks imply.
    Written after the loop and after the pull, so the file describes the folder as it ended up.

    A failed init does not get the file. The folder's creation is reported as failed, and writing
    ignores into a folder the portal is about to be told to delete (or rebuild) would leave the
    derived file as the only thing the operator made by hand and never asked for.
    """
    body = compose_gitignore(ran)
    path = _safe_join(target, ".gitignore")
    try:
        if os.path.exists(path):
            log.append({"step": "gitignore", "file": ".gitignore",
                        "result": "left the file that was already there"})
            return {"written": False, "kept": True, "sections": _sections(ran)}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:
        log.append({"step": "gitignore", "file": ".gitignore",
                    "result": f"failed: {type(exc).__name__}: {exc}"})
        return {"step": "gitignore", "command": None, "code": -1,
                "output": f"{type(exc).__name__}: {exc}"}
    log.append({"step": "gitignore", "file": ".gitignore",
                "result": "written from the boxes you picked"})
    return {"written": True, "kept": False, "sections": _sections(ran)}


def _sections(ran: list[str]) -> list[str]:
    """The stacks the file speaks for — the page names them, so the operator sees what the
    derivation was made from rather than trusting a file they did not ask for."""
    return [step for step in INIT_ORDER if step in ran and step in _GITIGNORE_STACKS]


def toolchains() -> dict[str, dict[str, Any]]:
    """What is on this box, measured now: path and version line. No assumed toolchains."""
    out: dict[str, dict[str, Any]] = {}
    for tool in TOOLS:
        found = shutil.which(tool)
        entry: dict[str, Any] = {"present": bool(found), "path": found}
        if found:
            entry["version"] = _version(found, tool)
        elif tool in INSTALL_HINT:
            entry["hint"] = INSTALL_HINT[tool]
        out[tool] = entry
    return out


def _version(binary: str, tool: str) -> str:
    argv = [binary, "--version"]
    if tool == "node":
        argv = [binary, "-v"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        line = (r.stdout or r.stderr).strip().splitlines()
        return line[0][:80] if line else ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"version unknown ({type(exc).__name__})"


def validate_name(name: Any) -> str:
    """A project name is a directory name. Every refusal quotes the same rule, so a page that shows
    one of them shows the rule the operator can type to."""
    rule = ("lowercase letters, digits and single hyphens, 2 to 64 characters, starting and ending "
            "with a letter or digit (my-app is fine; My-App, -app, app--1, a, app/x and .hidden are not)")
    if not isinstance(name, str):
        raise ProjectError(f"a project name must be a string. The rule: {rule}")
    raw = name.strip()
    if not raw:
        raise ProjectError(f"a project name cannot be empty. The rule: {rule}")
    if not _NAME.match(raw) or len(raw) < 2 or len(raw) > _MAX_NAME:
        raise ProjectError(f"{raw!r} is not a project name. The rule: {rule}")
    return raw


def validate_remote(url: Any) -> str:
    """A remote URL, validated before git ever sees it.

    The refusals are specific because a remote is not a string, it is an instruction to git:
    `ext::sh -c ...` is a working git remote that runs a command, and a URL carrying a password
    ends up readable by anything inside the project.
    """
    rule = ("an https://, http://, ssh://, git+ssh://, git:// or file:// URL, or the scp-like form "
            "user@host:path, with no spaces and at most 500 characters")
    if not isinstance(url, str):
        raise ProjectError(f"a remote must be a string. The rule: {rule}")
    raw = url.strip()
    if not raw:
        raise ProjectError(f"a remote cannot be an empty value. The rule: {rule}")
    if len(raw) > _REMOTE_MAX:
        raise ProjectError(f"a remote is {len(raw)} characters, over the {_REMOTE_MAX} limit")
    if any(c.isspace() for c in raw):
        raise ProjectError(f"{raw!r} contains a space. The rule: {rule}")
    low = raw.lower()
    if low.startswith("ext::"):
        raise ProjectError(
            f"{raw!r} is refused: git's ext:: transport runs a command to reach the remote, "
            f"and the portal will not hand git a command. The rule: {rule}"
        )
    if low.startswith(_REMOTE_SCHEMES):
        rest = raw.split("://", 1)[1]
        if not rest.strip("/"):
            raise ProjectError(f"{raw!r} has a scheme and no repository path. The rule: {rule}")
        authority = rest.split("/", 1)[0]
        userinfo = authority.rsplit("@", 1)[0] if "@" in authority else ""
        if ":" in userinfo:
            raise ProjectError(
                f"{raw!r} carries a password in the URL. Refused: git stores it in the project's "
                f".git/config in the clear, where the project's own code can read it. Use ssh://, "
                f"or https:// with a credential helper"
            )
        return raw
    if _SCP_LIKE.match(raw):
        return raw
    raise ProjectError(f"{raw!r} is not a git remote. The rule: {rule}")


def validate_texts(texts: Any) -> dict[str, str]:
    """The operator's opening text for README.md and AGENTS.md, keyed by step id.

    Empty text is no text: the step's own template is used. Supplying text implies the box, so a
    project created with only a README sentence still gets the file.
    """
    if texts is None:
        return {}
    if not isinstance(texts, dict):
        raise ProjectError("project text must be an object keyed by " + ", ".join(_TEXT_FILES))
    out: dict[str, str] = {}
    for key, value in texts.items():
        if key not in _TEXT_FILES:
            raise ProjectError(
                f"{key!r} is not a file the portal writes text into; it writes "
                f"{', '.join(_TEXT_FILES.values())}"
            )
        if not isinstance(value, str):
            raise ProjectError(f"the text for {_TEXT_FILES[key]} must be text")
        body = value.replace("\r\n", "\n").strip()
        if not body:
            continue
        if len(body) > _TEXT_MAX:
            raise ProjectError(
                f"the text for {_TEXT_FILES[key]} is {len(body)} characters, over the {_TEXT_MAX} "
                f"limit — that is a document, not a first entry"
            )
        out[key] = body
    return out


def remote_default_branch(url: str, cwd: str, env: dict[str, str]) -> str | None:
    """The remote's own default branch, read from the remote. `git pull origin main` on a remote
    whose default is `master` is a failure the page would have to explain; this reads the answer."""
    r = _run(["git", "ls-remote", "--symref", url, "HEAD"], cwd, env)
    if r["code"] != 0:
        return None
    for line in (r["stdout"] or "").splitlines():
        if line.startswith("ref: refs/heads/"):
            # the line is `ref: refs/heads/main<TAB>HEAD`; the branch is the first token
            tail = line[len("ref: refs/heads/"):].split()
            return tail[0] if tail and tail[0] != "HEAD" else None
    return None


def plan_init(steps: Any, tools: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Split the requested steps into what will run and what is refused, with the reason.

    A step whose toolchain is missing is refused *by name with the install command*, and the
    remaining steps still run — half a project the operator asked for, with the missing half
    written down, is a better outcome than nothing.
    """
    tools = tools or toolchains()
    if not isinstance(steps, list):
        raise ProjectError("init steps must be a list of step ids")

    run: list[str] = []
    refused: list[dict[str, str]] = []
    seen: set[str] = set()
    for step in steps:
        if step not in INIT_STEPS:
            raise ProjectError(
                f"unknown init step {step!r}; the page offers "
                f"{', '.join(INIT_ORDER)}"
            )
        if step in seen:
            continue
        seen.add(step)
        spec = INIT_STEPS[step]
        missing = [t for t in spec["needs"] if not tools.get(t, {}).get("present")]
        if missing:
            hints = [tools.get(t, {}).get("hint") or f"{t} is not installed" for t in missing]
            refused.append({"step": step, "label": spec["label"], "reason": "; ".join(hints)})
            continue
        run.append(step)

    run.sort(key=INIT_ORDER.index)
    return {
        "ok": True,
        "run": run,
        "refused": refused,
        # The file the boxes imply, named in the plan: the operator sees the ignores, and the
        # sections the ticked stacks add, before the folder exists.
        "gitignore": {"sections": _sections(run), "base": len(_GITIGNORE_BASE)},
    }


def project_path(name: str, root: str | None = None) -> str:
    root = root or portalsettings.projects_root()
    return os.path.join(root, name)


def create(name: Any, steps: Any, root: str | None = None, texts: dict | None = None,
           remote: str | None = None, pull_first: bool = False) -> dict[str, Any]:
    """Create a project folder, run the requested init steps, register it.

    Refusals happen before anything exists. Once the folder exists, a failing step is reported —
    not undone — because a half-built folder the page can name is a better state than a vanished one.

    The three additions the operator asked for, and how they order:

    - `texts` is the opening text for README.md / AGENTS.md. Supplying text implies the box.
    - `remote` is a validated git remote, added as `origin` right after `git init`. It implies git.
    - `pull_first` pulls the remote's own default branch **before the init files are written**, so
      the repository's README wins and the portal's templates fill only the gaps — the file loop
      below already leaves an existing file alone, which is exactly the merge rule wanted here.

    `.gitignore` is not a step the operator ticks. It is derived from the stacks that were picked
    (`compose_gitignore`) and written last, so it is present on a project that ticked git, one that
    did not, and one that arrived from a remote — and the same "leave the file that was already
    there" rule that governs the templates leaves the operator's own ignores alone.
    """
    safe_name = validate_name(name)
    texts = validate_texts(texts)
    remote_url = validate_remote(remote) if remote not in (None, "") else None
    # What the operator asked for implies the boxes: text for README.md means the README step, and
    # a remote means a git repository. The page sends the boxes it shows, so this is the API's
    # honesty, not the page's.
    if isinstance(steps, list):
        wanted = list(steps)
        wanted += [k for k in texts if k not in wanted]
        if remote_url and "git" not in wanted:
            wanted.append("git")
        steps = wanted
    root = root or portalsettings.projects_root()
    check = portalsettings.validate_projects_root(root)
    if not check["ok"]:
        raise ProjectError(f"the projects root is not usable: {check['error']}")

    target = project_path(safe_name, root)
    if os.path.realpath(target) != os.path.realpath(os.path.join(root, safe_name)):
        raise ProjectError(f"{target} resolves outside the projects root")
    if os.path.exists(target):
        raise ProjectError(
            f"{target} already exists. Use 'Adopt' to add the existing folder to the registry, "
            f"or pick a different name"
        )

    plan = plan_init(steps)
    try:
        os.makedirs(target, mode=0o755)
    except OSError as exc:
        raise ProjectError(f"{target} could not be created: {type(exc).__name__}: {exc}") from exc

    # The operator's words, not the template. Expanding ${name} first keeps the heading useful.
    specs: dict[str, dict[str, Any]] = {k: dict(v) for k, v in INIT_STEPS.items()}
    for step, body in texts.items():
        specs[step]["files"] = dict(specs[step].get("files") or {})
        specs[step]["files"][_TEXT_FILES[step]] = body

    log: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    remote_state: dict[str, Any] | None = None
    for step in plan["run"]:
        spec = specs[step]
        try:
            for d in spec.get("dirs", []):
                os.makedirs(_safe_join(target, _expand(d, safe_name)), exist_ok=True)
            for f, body in (spec.get("files") or {}).items():
                p = _safe_join(target, f)
                if os.path.exists(p):
                    log.append({"step": step, "file": f, "result": "left the file that was already there"})
                    continue
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(_expand(body, safe_name))
                log.append({"step": step, "file": f, "result": "written"})
        except OSError as exc:
            failed = {"step": step, "command": None, "code": -1,
                      "output": f"{type(exc).__name__}: {exc}"}
            break
        for argv in spec.get("commands", []):
            argv = [_expand(a, safe_name) for a in argv]
            # `git commit` needs an identity; a fresh box has none, and an init that dies on
            # "please tell me who you are" is a failed project the page cannot explain.
            env = dict(os.environ)
            env.setdefault("GIT_TERMINAL_PROMPT", "0")
            r = _run(argv, target, env)
            log.append({"step": step, "command": argv, **r})
            if r["code"] != 0:
                failed = {"step": step, "command": argv, "code": r["code"], "output": r["stderr"][:600]}
                break
        if step == "git" and remote_url and remote_state is None:
            # a remote that does not answer is not a reason to stop building the folder
            remote_state = _wire_remote(target, remote_url, bool(pull_first), log)
        if failed:
            break

    gitignore_state: dict[str, Any] | None = None
    if failed is None:
        gitignore_state = _write_gitignore(target, plan["run"], log)
        if gitignore_state and "code" in gitignore_state:
            failed = gitignore_state

    remote_failed = remote_state.get("error") if remote_state else None

    register(target, safe_name, notes={
        "init": plan["run"],
        "refused": plan["refused"],
        "remote": remote_state,
        "texts": sorted(_TEXT_FILES[k] for k in texts),
        "gitignore": gitignore_state,
    })

    return {
        "ok": failed is None and remote_failed is None,
        "name": safe_name,
        "path": target,
        "root": root,
        "ran": plan["run"],
        "refused": plan["refused"],
        "log": log,
        "failed": failed,
        "remote": remote_state,
        "gitignore": gitignore_state,
        "warning": _warning(target, failed, remote_state),
    }


#: What to do about a failed step, in the sentence the page shows. The remote is its own case:
#: a project whose remote did not attach is a working local project, not a half-created one.
_RECOVER = {
    "remote": "The remote is not configured; the project is usable without it, and "
              "'Adopt' is not needed — add it again from the project's row once the cause is fixed.",
    "default": "Fix the cause and tick the step again, or delete the folder.",
}


def _wire_remote(target: str, url: str, pull: bool,
                 log: list[dict[str, Any]]) -> dict[str, Any]:
    """Add the remote as `origin`, and optionally pull its default branch first.

    The default branch is read from the remote (`ls-remote --symref`), not assumed: `git pull origin
    main` against a repository whose default is `master` is a failure the page would have to explain.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    state: dict[str, Any] = {"url": url, "name": "origin", "added": False,
                             "branch": None, "pulled": False, "error": None}
    branch = remote_default_branch(url, target, env)
    log.append({"step": "remote", "command": ["git", "ls-remote", "--symref", url, "HEAD"],
                "code": 0 if branch else -1, "branch": branch})
    if not branch:
        state["error"] = {
            "step": "remote", "command": ["git", "ls-remote", url], "code": -1,
            "note": "the remote did not answer",
            "output": f"git ls-remote found no default branch at {url}. A private repository needs "
                      f"this box's ssh key or credential helper; a typo needs the URL fixed"}
        return state
    r = _run(["git", "remote", "add", "origin", url], target, env)
    log.append({"step": "remote", "command": ["git", "remote", "add", "origin", url], **r})
    if r["code"] != 0:
        state["error"] = {"step": "remote", "command": ["git", "remote", "add", "origin", url],
                          "code": r["code"], "output": r["stderr"][:600]}
        return state
    state["added"] = True
    state["branch"] = branch
    if not pull:
        return state
    argv = ["git", "pull", "--no-rebase", "--allow-unrelated-histories", "-q", "origin", branch]
    r = _run(argv, target, env)
    log.append({"step": "remote", "command": argv, **r})
    if r["code"] != 0:
        state["error"] = {"step": "remote", "command": argv, "code": r["code"],
                          "output": r["stderr"][:600],
                          "note": f"origin was added and points at {branch}; the pull did not complete"}
        return state
    state["pulled"] = True
    return state


def _warning(target: str, failed: dict | None, remote_state: dict | None) -> str | None:
    """One sentence for the page: what failed, what survived, and what to do next."""
    if failed is None and remote_state and remote_state.get("error"):
        err = remote_state["error"]
        return (f"{target} was created with its files. The remote did not attach "
                f"({err.get('note') or ' '.join(err['command'])} exited {err['code']}). "
                f"{_RECOVER['remote']}")
    if failed is None:
        return None
    return (f"{target} was created and step {failed['step']} failed "
            f"({' '.join(failed['command'])} exited {failed['code']}). "
            f"The files written before it are left in place — nothing was deleted. "
            f"{_RECOVER.get(failed['step'], _RECOVER['default'])}")


def _expand(text: str, name: str) -> str:
    return text.replace("${name}", name)


def _safe_join(base: str, part: str) -> str:
    """Join, and refuse anything that escapes `base`.

    The init steps' directory and file names are constants in `INIT_STEPS`, so nothing here is
    operator input. The check exists because the next step list will not be constants: the shape of
    this function is the shape that keeps a name from becoming a path.
    """
    if not part or part.startswith("/") or ".." in part.split("/") or "\x00" in part:
        raise ProjectError(f"{part!r} is not a name the portal will write inside {base}")
    joined = os.path.join(base, part)
    if os.path.dirname(os.path.realpath(joined)) != os.path.realpath(base):
        raise ProjectError(f"{part!r} resolves outside {base}")
    return joined


def _run(argv: list[str], cwd: str, env: dict[str, str]) -> dict[str, Any]:
    """One command, bounded: list argv (no shell), pinned cwd, closed stdin, killed on timeout."""
    started = time.time()
    try:
        r = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL, start_new_session=True)
        return {"code": r.returncode, "stdout": (r.stdout or "")[-600:],
                "stderr": (r.stderr or "")[-600:], "seconds": round(time.time() - started, 2)}
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="ignore")[-400:] if isinstance(exc.stdout, bytes) else ""
        return {"code": -1, "stdout": out, "stderr": "the command was killed after 180s",
                "seconds": round(time.time() - started, 2)}
    except OSError as exc:
        return {"code": -1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.time() - started, 2)}


# ------------------------------------------------------ registry + scan ----------------------

def register(path: str, name: str = "", notes: dict | None = None) -> dict[str, Any]:
    """Add a folder to the registry. Adoption is validated like creation.

    The name defaults to the folder's own: the registry records the folder name, so a caller that
    has a path has a name, and making the caller repeat it invites a registry entry that disagrees
    with the folder it points at.
    """
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise ProjectError(f"{real} is not a folder, so it cannot be a project")
    safe = validate_name(name or os.path.basename(real))
    if os.path.basename(real) != safe:
        raise ProjectError(
            f"{real} is a folder named {os.path.basename(real)!r}; the registry records the folder "
            f"name, so the name must match it"
        )
    db.execute(
        "INSERT INTO projects (path, name, created, last_opened, notes) VALUES (?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET name=excluded.name, notes=excluded.notes",
        (real, safe, time.time(), None, json.dumps(notes or {})),
    )
    return {"ok": True, "path": real, "name": safe}


def adopt(path: Any, name: Any = None) -> dict[str, Any]:
    """Adopt an existing folder (one the operator made by hand, with git clone, or pi made)."""
    if not isinstance(path, str) or not path.strip():
        raise ProjectError("an adopted project needs a folder path")
    real = os.path.realpath(os.path.expandvars(os.path.expanduser(path.strip())))
    root = os.path.realpath(portalsettings.projects_root())
    check = portalsettings.validate_projects_root(root)
    if not check["ok"]:
        raise ProjectError(f"the projects root is not usable: {check['error']}")
    if os.path.realpath(real) == root:
        raise ProjectError("that is the projects root itself, not a project inside it")
    if not real.startswith(root + os.sep):
        raise ProjectError(
            f"{real} is not inside the projects root ({root}). Move the folder in, or change the "
            f"projects root in Settings — the portal will not register a project it cannot list"
        )
    return register(real, validate_name(name) if name else None)


def scan(root: str | None = None) -> list[dict[str, Any]]:
    """Folders in the projects root that the registry does not know about."""
    root = os.path.realpath(root or portalsettings.projects_root())
    known = {row["path"] for row in db.query("SELECT path FROM projects")}
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for entry in entries:
        if entry.startswith("."):
            continue
        full = os.path.realpath(os.path.join(root, entry))
        if not os.path.isdir(full) or full in known:
            continue
        out.append({"path": full, "name": entry, "adoptable": bool(_NAME.match(entry)),
                    "reason": None if _NAME.match(entry) else "the folder name is not a project name"})
    return out


def list_projects() -> list[dict[str, Any]]:
    """The registry, each row enriched with the facts the page shows."""
    rows = db.query("SELECT path, name, created, last_opened, notes FROM projects ORDER BY name")
    return [_describe(dict(r)) for r in rows]


def touch(path: str) -> None:
    db.execute("UPDATE projects SET last_opened=? WHERE path=?", (time.time(), os.path.realpath(path)))


def remove(path: str) -> dict[str, Any]:
    """Forget a project. Deletes nothing on disk — that is the operator's verb, not the portal's."""
    real = os.path.realpath(path)
    db.execute("DELETE FROM projects WHERE path=?", (real,))
    return {"ok": True, "path": real, "deleted_from_disk": False,
            "note": "the folder is untouched; the registry no longer lists it"}


def _describe(row: dict[str, Any]) -> dict[str, Any]:
    """One registry row, enriched. `row` is already a plain dict: `sqlite3.Row` has no `.get`, and a
    helper that works on the row type by accident breaks the first time a dict arrives."""
    out = dict(row)
    p = row["path"]
    exists = os.path.isdir(p)
    out["exists"] = exists
    facts: dict[str, bool] = {}
    if exists:
        for key, needle in (("git", ".git"), ("readme", "README.md"), ("agents", "AGENTS.md"),
                            ("venv", ".venv"), ("node_modules", "node_modules"),
                            ("cmake", "CMakeLists.txt"), ("pyproject", "pyproject.toml"),
                            ("package_json", "package.json")):
            facts[key] = os.path.exists(os.path.join(p, needle))
        out["dirty_worktree"] = None
    out["facts"] = facts
    try:
        out["notes"] = json.loads(row.get("notes") or "{}")
    except (TypeError, ValueError):
        out["notes"] = {}
    return out


def summary() -> dict[str, Any]:
    """The projects card's facts: root, what is in it, what is missing from it."""
    root = portalsettings.projects_root()
    check = portalsettings.validate_projects_root(root)
    return {"root": root, "root_ok": check["ok"], "root_error": check.get("error"),
            "registered": len(list_projects()), "unadopted": len(scan(root)),
            "home": portalsettings.home(), "portal_home": portalsettings.portal_home(),
            "model_library": str(paths.MODELS_ROOT)}
