# ADR 001: Small local model, containerized, local MCP

Status: Accepted

## Context

Most coding agents assume a large model, a long context window, and a network path to someone else's GPUs. TinyLocalCoder is for the other case: a small model on the machine you already have, no API key, and other local tools able to drive it. See [Architecture](../Architecture.md) and [why TinyLocalCoder](../why-tiny-local-coder.md).

A model that small cannot hold a whole plan, a whole tree, or a long chat. The product is the system around the model.

## Decision

Three choices define the product:

1. **A small local language model.** The default is Ollama (for example `qwen2.5:3b`) in the suite's own container. [LM Studio](../../config.toml) on the host is the other supported server, and only when it is already running locally. No GPU is required. Prompts stay short: one todo per call. How that stays true is [005](005-DiskMemory.md), [006](006-DeterministicStructure.md), and [007](007-ApprovalGate.md).
2. **The suite runs in containers.** `./start-service.sh` starts the app image and, for an Ollama model, the Ollama container. Weights live in the external volume `crew_pipeline_ollama_data` and survive stop and rebuild. The Linux userspace those containers provide is [002](002-ContainerFirst.md).
3. **The app serves MCP on the same process as the API.** Streamable HTTP at `http://127.0.0.1:8000/mcp`. Other agents on the machine use that endpoint. How CLI, MCP, and REST share the session is [003](003-Interoperability.md).

## Consequences

- Cost is electricity and RAM on this machine. Multi-file reasoning that needs a frontier model is outside what this system is for.
- Docker is required. A Mac host needs a Linux VM with enough RAM for the chosen model (about 12 GB for the default).
- Supported model servers are local. Pointing `base_url` at a remote host sends prompts off the machine and leaves this decision.
- A larger `NUM_CTX` buys a larger file slice beside the current todo ([005](005-DiskMemory.md)). The prompt shape stays one todo.
