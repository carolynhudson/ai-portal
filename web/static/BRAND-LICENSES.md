# Third-party marks served from this directory

`third-party/icon-dark.svg`
: The llama.cpp icon, from <https://github.com/ggml-org/llama.brand>
  (`icon/icon-dark.svg`). Licensed **CC BY-NC 4.0**; the repo's
  `BRAND-USAGE.md` additionally grants reproduction "for the purpose of
  identifying or referring to Llama.app and Llama.cpp", which is exactly
  this use. The file is embedded **unmodified** (the only addition is
  this provenance comment, which is not part of the SVG markup).
  Attribution per the license: ggml-org/llama.brand.

`third-party/comfy-icon.svg`
: The ComfyUI icon, from <https://comfy.org/icons/comfyicon.svg>.
  "Comfy" and "ComfyUI" are **trademarks of Comfy Org**. Used under
  comfy.org/brand's guidelines: the logo is unmodified (no recolor, no
  outline, no distortion), it identifies the ComfyUI service running on
  this machine, and nothing here implies Comfy Org endorsement or
  partnership. The portal's own name is HALO, not a Comfy derivative.

Both files carry a provenance comment naming their upstream. The test
suite (`tests/test_ui_contract.py`) checks the served markup is
byte-identical to upstream when the network is reachable.
