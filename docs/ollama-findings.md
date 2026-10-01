# Why Bonsai 27B is not in the model picker

[prism-ml/Bonsai-27B-gguf](https://huggingface.co/prism-ml/Bonsai-27B-gguf) was considered as a third Ollama choice next to `qwen2.5:3b` and `qwen3.5:4b`. It stays out of the catalog. The startup list only offers models the pinned Ollama image can pull and load.

The image is `ollama/ollama:0.34.0` in `docker-compose.yml`. `./start-service.sh` runs `ollama pull` for the tag in `config.toml`, then the app talks to that daemon. A catalog row is a promise that this path works.

## What the Hugging Face repo actually ships

Bonsai 27B is a llama.cpp GGUF, derived from Qwen3.6-27B. The language-model file is `Bonsai-27B-Q1_0.gguf` (~3.9 GB). The same repo also contains files that are not the language model:

| File | What it is |
|------|------------|
| `Bonsai-27B-Q1_0.gguf` | The 1-bit language model |
| `Bonsai-27B-F16.gguf` | Full-precision reference weights |
| `Bonsai-27B-dspark-*.gguf` | Experimental speculative-decoding drafter |
| `Bonsai-27B-mmproj-*.gguf` | Optional vision tower |

The GGUF metadata reports `general.architecture = qwen35`. The weight type is `Q1_0` with group size 128 (also called `Q1_0_g128`): one sign bit per weight plus an FP16 scale every 128 weights, about 1.125 bits per weight. Prism's run instructions use their llama.cpp build (`llama-cli` / `llama-server`), on CPU, CUDA, or Metal. Ollama is not a listed backend.

## Why `ollama pull` does not make it runnable

**The quant type is missing from Ollama's engine.** Ollama ships its own ggml. That build's tensor-type enum ends before `Q1_0`, so loading `Bonsai-27B-Q1_0.gguf` aborts inside the runner (`GGML_ASSERT(type >= 0 && type < GGML_TYPE_COUNT)`, HTTP 500). The failure looks like a crash, and it is a missing kernel. Upstream llama.cpp grew `Q1_0` support in 2026 (CPU, then CUDA, Metal, and Vulkan). Ollama 0.34.0, released 2026-09-05, still does not load this pack. Prism's own note on the Bonsai 8B Ollama issue says to use their llama.cpp demo until Ollama picks the kernels up. A community proxy ([eslider/bonsai-ollama](https://github.com/eslider/bonsai-ollama)) exists for the same reason: it forwards Bonsai traffic to Prism's `llama-server` because stock `ollama run` cannot load `Q1_0`.

**The architecture string is the upstream llama.cpp name, not Ollama's.** Ollama's library tags `qwen3.5` and `qwen3.6` are models Ollama converted itself. A raw Hugging Face GGUF labeled `qwen35` (and the MoE sibling `qwen35moe`) has failed on Ollama with `unknown model architecture: 'qwen35moe'` when the vendored parser only recognized `qwen3next`. Bonsai's file is the dense `qwen35` form of that same split.

**A Hugging Face pull can select the wrong sibling.** `ollama run hf.co/prism-ml/Bonsai-27B-gguf:BF16` on Ollama 0.32.1 died with `unknown model architecture: 'dspark'`. Prism's reply on that thread: the dspark files are draft models, they only run in Prism's llama.cpp fork, and the language model to use is the `Q1_0` GGUF. Even a quant-pinned pull of `Q1_0` still hits the missing tensor type above.

An unofficial copy on ollama.com (`MobiusDevelopment/Bonsai-27B-Q1_0-gguf`) does not change this. The registry will accept a GGUF the runner cannot execute.

## What we did instead

The picker gained `lmstudio` (`http://localhost:1234/v1`) and did not gain a Bonsai row. Someone who wants these weights loads `Bonsai-27B-Q1_0.gguf` in LM Studio, or serves it with Prism's `llama-server`, and points Tiny Local Coder at that server. The Ollama container stays on models it can actually load.
