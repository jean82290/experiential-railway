# Text for the Railway template form

Copy each block into the matching field when you publish the template.

---

## Name

```
Experiential | AI Gateway with Per-Key Budgets
```

## Short description

```
OpenAI-compatible gateway for many LLMs, with per-agent keys and spend caps.
```

## Category

```
AI/ML
```

## Overview (Markdown)

```markdown
# Deploy and Host Experiential on Railway

Experiential is an open-source AI gateway. It puts OpenAI, Anthropic, Gemini, OpenRouter and your own OpenAI-compatible servers behind one OpenAI-compatible API (plus the Anthropic Messages API), and gives every user or agent its own virtual key, model access and monthly spending cap.

## About Hosting Experiential

This template runs the official Experiential release from PyPI, with every dependency pinned by SHA-256, as one service with a Railway volume at `/data` for the gateway database. Your provider keys stay in Railway variables: Experiential stores only their names.

Things a bare Experiential deploy gets wrong on Railway, handled here:

- **It would be unreachable.** Experiential only listens on 127.0.0.1. A Caddy front door in the same container listens on Railway's port.
- **It would leak spend data.** On loopback, its usage report and metrics answer without a key. Caddy exposes only the API and health checks; the usage page sits behind an admin password.
- **It would wait for a wizard.** Setup is interactive upstream. Here it is configured from variables on every boot: providers, one alias per model in `EXP_MODELS` (with capabilities, limits and prices from Experiential's own catalog), a `default` identity and its first key, printed once in the deploy logs.
- **It would phone home.** Anonymous telemetry is on by default upstream; this template turns it off.

## Common Use Cases

- One API endpoint and one bill view for all the LLM providers your team or app uses
- A separate key and monthly budget for each AI agent, so a runaway loop cannot drain your account
- Switching an app between OpenAI, Claude and open models by changing only the model name
- Putting self-hosted models (vLLM, Ollama) behind the same authenticated API as hosted ones

## Dependencies for Experiential Hosting

- Experiential (official PyPI release, github.com/experientiallabs/experiential, Apache-2.0)
- At least one model provider key (OpenAI, Anthropic, Gemini, OpenRouter) or an OpenAI-compatible server
- A Railway volume mounted at `/data`

### Deployment Dependencies

- Experiential repository: https://github.com/experientiallabs/experiential
- Experiential documentation: https://platform.experientiallabs.ai/docs
- Template source and smoke test: https://github.com/jean82290/experiential-railway

### Implementation Details

Use any OpenAI SDK with the gateway key from the deploy logs:

    OPENAI_BASE_URL=https://<your-domain>/v1
    OPENAI_API_KEY=<gateway key>

From another service in the same project, over the private network:

    OPENAI_BASE_URL=http://${{experiential.RAILWAY_PRIVATE_DOMAIN}}:${{experiential.PORT}}/v1

Add identities, keys and monthly budgets with `exp config gateway ...` from `railway ssh`; changes apply without a redeploy. Open `/usage` in a browser (user `admin`, password `EXP_ADMIN_PASSWORD`) to see spend per identity.

This is a community-maintained template, not affiliated with Experiential Labs.

## Why Deploy Experiential on Railway?

Railway is a singular platform to deploy your infrastructure stack. Railway will host your infrastructure so you don't have to deal with configuration, while allowing you to vertically and horizontally scale it.

By deploying Experiential on Railway, you are one step closer to supporting a complete full-stack application with minimal burden. Host your servers, databases, AI agents, and more on Railway.
```

## Service settings to enter in the template composer

| Setting | Value |
|---|---|
| Service name | `experiential` |
| Source | your GitHub repository (this one) |
| Volume mount path | `/data` |
| Public networking | HTTP, target port `8080` |
| Healthcheck path | `/health/ready` (also set by `railway.json`) |
| Restart policy | On failure (also set by `railway.json`) |

| Variable | Value | Notes |
|---|---|---|
| `PORT` | `8080` | |
| `EXP_MODELS` | `openai:gpt-5.6-terra,anthropic:claude-sonnet-5-5,gemini:gemini-3.6-flash` | |
| `EXP_ADMIN_PASSWORD` | `${{secret(32)}}` | |
| `OPENAI_API_KEY` | *(empty)* | optional; set at least one provider |
| `ANTHROPIC_API_KEY` | *(empty)* | optional |
| `GEMINI_API_KEY` | *(empty)* | optional |
| `OPENROUTER_API_KEY` | *(empty)* | optional |
