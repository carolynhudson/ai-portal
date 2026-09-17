"""Provider layer: one class per managed tool (llama.cpp, ComfyUI, Hugging Face)."""
from __future__ import annotations

from typing import TypeVar

from .base import Provider, ToolStatus
from .llamacpp import LlamaCpp
from .comfyui import ComfyUI
from .huggingface import HuggingFace

#: registration order == display order in the UI
PROVIDERS: list[Provider] = [LlamaCpp(), ComfyUI(), HuggingFace()]

_T = TypeVar("_T")


def get(key: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.key == key), None)


# ---------------------------------------------------------------- type-narrowed
# `get()` answers with the base type, but the callers do not want the base type:
# `models_live()` exists only on LlamaCpp and `config()` only on ComfyUI, so
# reaching them through a Provider was ~49 "attribute unknown" findings — and a
# real AttributeError if a key ever resolved to the wrong tool. Declaring those
# methods on the base would be the wrong cure: it would advertise a ComfyUI
# vision of `models_live()` and invite exactly the bug the types are pointing at.
# Narrowing at the accessor keeps each capability owned by the class that has it.
#
# The accessors are named get_* on purpose: a plain `comfyui` here would shadow the
# `app.providers.comfyui` submodule on the package namespace, and
# `from app.providers import comfyui` would hand back the function instead of the
# module — which is exactly how tests/test_comfyui_update.py broke on first try.
def _require(p: _T | None, key: str) -> _T:
    """A registered provider is a build-time fact, not a runtime one.

    `PROVIDERS` is a module literal, so a missing key means someone removed or
    renamed a registration — a programming error, not a state the server should
    be serving requests in. Raising here turns it into an import-time or
    first-request failure with the provider's name in it, which is both honest
    and far easier to diagnose than an AttributeError on some unrelated method.
    """
    if p is None:
        raise RuntimeError(f"provider {key!r} is not registered in PROVIDERS")
    return p


def get_llamacpp() -> LlamaCpp:
    """The llama.cpp provider, typed and non-None (see _require).

    isinstance() does two jobs here: it narrows the type for the checker, and it
    catches a registration that was pointed at the wrong class — which is what
    the "attribute unknown" findings were really warning about.
    """
    p = get("llamacpp")
    if isinstance(p, LlamaCpp):
        return p
    return _require(None, "llamacpp")


def get_comfyui() -> ComfyUI:
    """The ComfyUI provider, typed and non-None (see _require)."""
    p = get("comfyui")
    if isinstance(p, ComfyUI):
        return p
    return _require(None, "comfyui")


def get_huggingface() -> HuggingFace:
    """The Hugging Face provider, typed and non-None (see _require)."""
    p = get("huggingface")
    if isinstance(p, HuggingFace):
        return p
    return _require(None, "huggingface")
