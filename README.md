# caty-gateway

`caty-gateway` is a self-hosted gateway that connects Caty clients to a supported local or HTTP-based AI backend. The client and gateway must be on the same Tailscale network.

```sh
uv tool install caty-gateway && caty-gateway setup --member <id> --backend <claude|codex|openclaw|hermes|openai-compat>
```

More documentation is coming soon.
