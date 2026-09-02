"""Provider layer: one class per managed tool (llama.cpp, ComfyUI, Hugging Face)."""
from .base import Provider, ToolStatus
from .llamacpp import LlamaCpp
from .comfyui import ComfyUI
from .huggingface import HuggingFace

#: registration order == display order in the UI
PROVIDERS: list[Provider] = [LlamaCpp(), ComfyUI(), HuggingFace()]


def get(key: str) -> Provider | None:
    return next((p for p in PROVIDERS if p.key == key), None)
