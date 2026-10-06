# Aider — local coding assistant setup

Aider **0.82.3** is installed (via `py -3 -m pip install aider-chat`). It's a strong
alternative to OpenCode with real token/context visibility per message.

## Config (already in place)
Two files in your home dir (`C:\Users\me`):

- `.aider.conf.yml` — default model = `ollama_chat/coder` (the 32k-ctx MoE coder).
- `.aider.model.settings.yml` — **sets `num_ctx` per model.** This is critical: aider
  defaults Ollama to a 2048-token context, which causes the same "model forgets the
  task" truncation bug seen in OpenCode. Here every model gets a real context window.

## Launch (important)
- Run from **Windows Terminal or PowerShell** — NOT Git Bash (prompt_toolkit crashes
  under xterm). A modern terminal also fixes the "pretty output" warning.
- Run **inside the target project's git repo** (aider is git-native).

```powershell
$env:OLLAMA_API_BASE = "http://127.0.0.1:11434"   # optional; this is the default
cd C:\path\to\your\project
aider                                   # uses default ollama_chat/coder
aider --model ollama_chat/qwen2.5-coder:7b   # faster, simpler tasks
aider --model ollama_chat/architect          # big-context design work
```

## Which model
- Default `coder` (qwen3-coder:30b MoE, 32k) — best all-round, ~64 tok/s.
- `qwen2.5-coder:7b/14b` — faster for simple edits.
- Swap per session with `--model`. Remember: one big model resident at a time on 12 GB.

## Verified
`aider --message "..."` one-shot returned a correct reply and reported token counts —
full chain aider → litellm → Ollama confirmed working.
