# Windows agent — opencode on RainbowBlock, brain in the Halo router

The second lane of the forge plan: the Windows box gets **its own agent**
(opencode) that thinks with the Halo box's llama.cpp router over the LAN.
The kit in `../windows-forge` remains the human path; this lane is for
everything after: parse-error archaeology, install babysitting, upkeep —
the agent reads `AGENTS.md` (the mission brief: what to do, the hard
rules, the definition of done) and fixes the box itself.

## Why there is no "llama.cpp" provider in opencode

opencode ships a list of known providers; llama.cpp is not one of them,
and it doesn't need to be: a llama.cpp server (which is what the router
is) speaks the OpenAI wire protocol, so you **declare** it as an
OpenAI-compatible provider. `opencode.json` in this folder is exactly
that declaration — provider name `llamarouter`, `@ai-sdk/openai-compatible`,
`baseURL http://192.168.1.228:8080/v1`, four models (the small coders
that coexist with the portal; the 84 GB resident model is deliberately
absent, see below). The Desktop app and the TUI read the same config.

## Setup on RainbowBlock.local

1. **CLI** (the Desktop app is beta; the TUI shares its config and is the
   best-tested path with custom providers):
   ```powershell
   winget install OpenJS.NodeJS.LTS
   npm install -g opencode-ai
   ```
2. **Copy this folder's `opencode.json` and `AGENTS.md` into
   `C:\forge\kit`** (the forge kit folder). Project-local `opencode.json`
   scopes the config to the forge work; `AGENTS.md` is picked up the same
   way. For a global config, put `opencode.json` at
   `%USERPROFILE%\.config\opencode\opencode.json` instead.
3. **Link check:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\check_agent_link.ps1
   ```
   Expect the model list. (The router is already bound `0.0.0.0:8080` on
   this network — `ss` on the Halo box says so. If TCP fails, the router
   bind is the owner's Settings page, confirm-gated by design.)
4. **Start the session:**
   ```powershell
   cd C:\forge\kit
   opencode
   ```
   Model: the config default (`llamarouter/Jackrong/Qwopus3.5-4B-Coder…`).
   First message: `Read AGENTS.md and carry out the mission. Report at the end.`

## The honest trade-offs

* **Speed**: the 4 B coder is quick on the Halo; the 84 GB resident MoE
  (the model *you* talk to in the portal) is a few tok/s and reserved.
  The agent config omits it on purpose — requesting it evicts the
  controller's brain. Deeper problems: ask in the portal; the agent
  handles the hands-on work.
* **No secrets on this box**: the router is unauthenticated by design
  (LAN-only trust, the same rule as ComfyUI's firewall pin).
  `"apiKey": "local"` is a placeholder llama.cpp ignores. Nothing here
  is worth stealing, so nothing here can leak.
* **8080 is open to the LAN** (owner's long-standing bind): every LAN
  device can spend the models. Accepted state, noted in the portal's
  amber hero pill.
* **Model loads are visible**: when the agent switches models, the router
  loads them (portal dashboard: model swap, RAM moving). The listed
  models coexist with the resident MoE; stay on the list.

## Files

| file | what |
|---|---|
| `opencode.json` | the provider declaration + model picks + default |
| `AGENTS.md` | the mission brief: steps, hard rules, definition of done |
| `check_agent_link.ps1` | TCP + `/v1/models` reachability from Windows |
