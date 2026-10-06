# Ollama flash attention + KV cache q8_0 — falsified (2026-07-15)

**Question:** does `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`
buy tok/s on the resident setup (qwen3-coder:30b Q4 = `coder:latest`,
RTX 5070 12 GB, model split ~50/50 CPU/GPU, Ollama 0.31.2, Windows)?

**Answer: no. Identical within noise. Keep stock config.**

| config | gen short (tok/s) | gen 8k-prompt | prefill cold (tok/s) |
|---|---|---|---|
| stock, 32k ctx | 62.5–62.7 | 53.1–55.0 | ~2590 |
| FA + KV q8, 32k ctx | 62.9–63.4 | 54.1 | ~2580 |
| stock, 64k ctx | — | 49.0–50.4 | ~2330 |
| FA + KV q8, 64k ctx | — | 48.7–50.4 | ~2330 |

Reading: the bottleneck is the CPU-offloaded MoE experts, not attention math
or KV-cache size. Ollama's layer split did not change under q8 KV (still
50/50, 20 GB), so no layers moved back to the GPU. The remaining perf lever
on this box is expert-level offload (`--n-cpu-moe`) — that is the
llama-server bench in the roadmap, not an Ollama env flag.

Useful side-finding: **64k ctx costs only ~7 % gen speed vs 32k**
(55 → 50 tok/s). Bumping an agent session to 64k when it needs the room is
cheap.

## Methodology traps (worth remembering)

1. **`Stop-Process` on `ollama`/`ollama app` leaks `llama-server.exe`
   children** which keep their VRAM. First measurement pass had up to four
   zombies fighting over 12 GB and produced garbage numbers (4–39 tok/s)
   that looked like a dramatic FA regression. Always
   `Stop-Process -Name 'ollama app','ollama','llama-server'` and check
   `nvidia-smi` before measuring.
2. **`setx` does not reach the child**: the app was started from a shell
   whose own env never had the vars. Set them in-process
   (`$env:X=...; Start-Process ...`) and verify in `server.log`
   ("server config" line prints the whole env map).
3. Leftover from the 2026-07-13 vandalism found during cleanup: the Ollama
   desktop app's `db.sqlite` `settings.context_length` was **262144** (any
   client not passing num_ctx allocated a 256k KV cache). Reset to 32768.
