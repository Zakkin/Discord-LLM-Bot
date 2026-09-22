# Qwen3.6 Ollama Modelfiles

Qwen3.6 uses thinking mode by default. These Modelfiles intentionally do not override
the embedded Qwen3.6 GGUF chat template, so the bot can control thinking through the
Ollama API `think` field.
Do not add `/think` or `/nothink` prompt suffixes for Qwen3.6; use the runtime
reasoning switch instead. In llama.cpp-style servers this is `--reasoning off`;
in this bot's Ollama path it is the per-request `think: false` flag.

Create the two persona models on the GPU server after placing the GGUF file at the
path used by each Modelfile:

```bash
ollama create shanks-local-qwen36-a3b-jpn -f modelfiles/shanks-qwen36.Modelfile
ollama create img-local-qwen36-a3b-jpn -f modelfiles/img-qwen36.Modelfile
```

The default GGUF path is:

```text
/opt/Qwen3.6-35B-A3B-Q4_K_M/Qwen3.6-35B-A3B-Q4_K_M.gguf
```

If KV cache memory allows it, `num_ctx` can be raised from `8192` to `16384` or
`32768` for longer web research and memory-heavy conversations.

## Keep-alive settings

`keep_alive` is an **API request field**, not a valid Modelfile `PARAMETER`.
Set it via environment variables (`OLLAMA_CLASSIFIER_KEEP_ALIVE`, `OLLAMA_MIDDLE_KEEP_ALIVE`, etc.)
which the bot injects per request via `_resolve_keep_alive()`.

With 16 GB VRAM, models cannot all be resident simultaneously.
The strategy is:

| Model | keep_alive | Reason |
|---|---|---|
| `shanks-local-qwen36-a3b-jpn` | `-1` (env: `OLLAMA_MAIN_KEEP_ALIVE=-1`) | Main persona; always resident |
| `img-local-qwen36-a3b-jpn` | `0` | Loaded on demand; shares VRAM slot with shanks |
| `middle-llm` | `120` (default) | Called 2–3 times per message; 120s window lets sequential calls share one load |
| `classifier-local-v2` | `0` | **Deprecated as default**: `OLLAMA_CLASSIFIER_MODEL` now defaults to `middle-llm` to avoid loading a third model per message |

To apply Modelfile changes to the classifier and middle-llm models on the server:

```bash
ollama create classifier-local-v2 -f modelfiles/classifier-v2.Modelfile
ollama create middle-llm -f modelfiles/middle-llm.Modelfile
```

