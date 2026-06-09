# Central embedding service (Ollama)

One shared Ollama serves embeddings for **every** belleq host, instead of each
master running its own (which was unreliable and wasteful).

## Deploy (once)

1. Launch a dedicated instance — **t3.large** recommended (embeddings are
   CPU-bound; nomic-embed-text is small). Same region/VPC as your hosts.
2. Install Docker + compose, then:
   ```bash
   docker compose up -d
   ```
   The `model-pull` one-shot fetches `nomic-embed-text` on first boot.
3. Verify:
   ```bash
   curl -s http://localhost:11434/api/tags | jq      # lists nomic-embed-text
   curl -s http://localhost:11434/api/embeddings \
     -d '{"model":"nomic-embed-text","prompt":"hello"}' | jq '.embedding | length'   # 768
   ```

## Wire it up

In the **platform backend** `.env`:
```
EMBEDDING_OLLAMA_URL=http://<this-host-private-ip>:11434
EMBEDDING_MODEL=nomic-embed-text
```
New hosts pick this up automatically — the bootstrap writes `OLLAMA_BASE_URL`
into the master's `.env`, and the master passes it to every context container.

## Security

Restrict inbound **11434** to your belleq hosts' security group only — do not
expose it to the public internet.
