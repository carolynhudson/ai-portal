"""Model configs: a named bundle of llama.cpp settings for one library model (§4.7).

A config is `(name, base_model, settings)`. It exists because the same model file wants
different treatment in different personalities, and because the name a model file was shipped
with is not necessarily the name a person wants to type.

The name is not cosmetic. `personality.render()` writes it into the base model's own preset
section as `alias = <name>`, and the router treats that as a routable name — measured on this
box against b10964:

    [Qwen3.5-0.8B.Q4_K_M]
    alias = tiny-chat, quick

    GET /models   -> id='Qwen3.5-0.8B.Q4_K_M' aliases=['quick', 'tiny-chat']
    POST /v1/chat/completions {"model": "tiny-chat"}  -> 200

so the portal's name becomes the name the router answers to. Preset *sections* are still keyed
by the model's real directory name (`server-models.cpp` cascades `[*]`, then the section named
after the model, then the router's own CLI args), which is why a config never replaces the
section name — it only labels the model inside it.

That measurement also fixes the validation rules. The router keeps aliases in one namespace
shared by every model, and a collision is not a warning:

    alias = Qwythos-9B-Claude-Mythos-5-1M-Q4_K_M   on another model's section
    -> exit 1: "failed to initialize router models: model name '...' conflicts with
       alias of model '...'"

A name that collides therefore does not fail to apply, it stops the router from starting. Hence
this module validates names against every library model id and every other config name *before*
anything can be written, and restricts the alphabet to characters that cannot break an ini line
or a comma-separated alias list.
"""
from __future__ import annotations

import json
import re
import time

from . import db, modelindex

#: A config name has to survive three things at once: an ini value (`x = <name>`), the router's
#: comma-split of `--alias`, and being rendered into the page. Letters, digits, dot, dash and
#: underscore clear all three; spaces, commas, quotes and `*` do not. `*` in particular is the
#: global section's own name, so a config called `*` would collide with `[*]` itself.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,47}")

MAX_NAME_LEN = 48

#: Reserved words that are not config names: `[*]` is the global section, and the two built-in
#: personalities are computed, so a config may not pretend to be one either.
RESERVED = ("*", "safe", "default")


class ConfigError(ValueError):
    """A config that the portal will not store. Refused rather than sanitised: a name silently
    rewritten is a name the operator will not find in the file afterwards."""


def _decode(row) -> dict:
    out = dict(row)
    try:
        out["settings"] = json.loads(out.get("settings") or "{}")
    except (TypeError, ValueError):
        # A corrupt row is reported, not repaired. Silently reading it as "no settings" would make
        # a config that used to carry ctx-size look like a config that never did.
        out["settings"] = {}
        out["settings_corrupt"] = True
    return out


def rows() -> list[dict]:
    """Every stored config, settings decoded."""
    return [_decode(r) for r in db.query(
        "SELECT * FROM model_configs ORDER BY name")]


def get(name: str) -> dict | None:
    row = db.query("SELECT * FROM model_configs WHERE name = ?", (name,))
    return _decode(row[0]) if row else None


def bases() -> list[str]:
    """Library model ids, straight from the index — the only things a config may be built on."""
    return [r["id"] for r in modelindex.rows()]


def name_problem(name: str, base_model: str | None = None,
                 allow_self: bool = False) -> str | None:
    """Why this name cannot be used, or None if it can.

    `allow_self` is for updating a config in place: its own row must not collide with itself.
    """
    if not isinstance(name, str) or not name.strip():
        return "a model config needs a name"
    if name != name.strip():
        return "a model config name cannot start or end with whitespace"
    if len(name) > MAX_NAME_LEN:
        return f"a model config name is at most {MAX_NAME_LEN} characters"
    if not NAME_RE.fullmatch(name):
        return ("a model config name may use letters, digits, dot, dash and underscore, starting "
                "with a letter or digit (the router splits aliases on commas, and ini values end "
                "at a newline)")
    if name in RESERVED:
        return f"{name!r} is reserved: it is the global section or a built-in personality"

    library = set(bases())
    if name in library:
        return (f"{name!r} is already a model in the library; the router refuses a name that "
                f"collides with a model and would not start")
    if base_model is not None and name == base_model:
        return "a config name cannot be its own base model's name — that section already exists"

    for other in db.query("SELECT name, base_model FROM model_configs"):
        if other["name"] == name:
            if allow_self:
                continue
            return (f"{name!r} is already a config for {other['base_model']!r}; aliases share one "
                    f"namespace across all models, so the router would refuse to start")
        # Two configs for the same base model are allowed (different settings, different names),
        # but they must not alias the same model twice under names that clash later.
    return None


def save(name: str, base_model: str, settings: dict, note: str = "") -> dict:
    """Insert or update one config. Assumes the settings have already been validated.

    Storage validates only what storage must not accept blindly: a usable name and a base model
    that actually exists in the library. A config pointing at a model that is not there would
    render a section the router ignores.
    """
    problem = name_problem(name, base_model, allow_self=True)
    if problem:
        raise ConfigError(problem)

    # `allow_self` above is only about not colliding with your own row. It must not become a
    # licence to retarget: a config named `tiny-chat` that used to rename the 0.8B and now renames
    # the 9B would leave every personality that assigned `tiny-chat` pointing at a different model,
    # with the same name and the same settings to prove it. A config's base model is fixed when it
    # is created; naming a different model means a different config.
    prior = db.query("SELECT base_model FROM model_configs WHERE name = ?", (name,))
    if prior and prior[0]["base_model"] != base_model:
        raise ConfigError(
            f"{name!r} already configures {prior[0]['base_model']!r}; a config does not change which "
            f"model it names, because every personality assigning it would follow it to the new one "
            f"-- save a config for {base_model!r} under a new name instead")

    if not base_model:
        raise ConfigError("a model config needs a base model")
    if base_model not in set(bases()):
        raise ConfigError(f"base model {base_model!r} has no library entry, so the router would "
                          f"ignore a section for it")
    now = time.time()
    existing = db.query("SELECT created_at FROM model_configs WHERE name = ?", (name,))
    created = existing[0]["created_at"] if existing else now
    db.execute(
        """INSERT INTO model_configs (name, base_model, settings, note, created_at, updated_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET
             base_model=excluded.base_model, settings=excluded.settings,
             note=excluded.note, updated_at=excluded.updated_at""",
        (name, base_model, json.dumps(settings, sort_keys=True), note, created, now))
    db.event("portal", "model-config", f"saved {name} for {base_model} "
                                       f"({len(settings)} setting(s))")
    return get(name)


def delete(name: str) -> bool:
    row = db.query("SELECT base_model FROM model_configs WHERE name = ?", (name,))
    if not row:
        return False
    db.execute("DELETE FROM model_configs WHERE name = ?", (name,))
    db.event("portal", "model-config", f"deleted {name} (was {row[0]['base_model']})")
    return True


def assigned_personalities(name: str) -> list[str]:
    """Personalities that currently use this config.

    Deleting a config out from under a personality would not fail loudly: the personality would
    simply stop carrying those settings, and the next apply would write a smaller file than the
    operator believes they are editing. So the question is asked before the delete, by the caller,
    and this is where the answer comes from.
    """
    hits: list[str] = []
    for row in db.query("SELECT name, models FROM personalities"):
        try:
            models = json.loads(row["models"]) if row["models"] else None
        except (TypeError, ValueError):
            continue
        if models and name in models:
            hits.append(row["name"])
    return hits
