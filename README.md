# Experiential gateway on Railway

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/xCp09Y?referralCode=NamAm4&utm_medium=integration&utm_source=template&utm_campaign=generic)

One-click [Experiential](https://github.com/experientiallabs/experiential) on Railway: an open-source AI gateway that puts OpenAI, Anthropic, Gemini, OpenRouter and your own OpenAI-compatible servers behind one OpenAI-compatible API (and the Anthropic Messages API), with a virtual key per user or agent, per-key model access, and monthly spending caps.

This is a community-maintained deployment package. It is not affiliated with or endorsed by Experiential Labs.

## What this template deploys

| | |
|---|---|
| Service | `experiential`, built from this repository's `Dockerfile` |
| Software | The official Experiential release from PyPI, pinned with every dependency locked by SHA-256 (`requirements.txt`) |
| Front door | Caddy (official release, pinned by SHA-512) on Railway's port, in the same container |
| Storage | A Railway volume at `/data` (gateway database, keys, usage, budgets) |
| Public URL | HTTPS on Railway's domain: `/v1` for the API, `/usage` for spend |
| Health check | `GET /health/ready` |
| Telemetry | Experiential's anonymous usage statistics are **off** (`EXP_TELEMETRY=0`) |

Out of the box Experiential cannot run on Railway, and this template fixes each point:

1. **It only listens on `127.0.0.1`**, by design, so Railway cannot reach it. Caddy listens on Railway's port and forwards to it.
2. **It trusts whatever reaches that loopback address.** Its organization-wide usage report and its metrics answer without any key. Caddy forwards only the API, the health checks and, behind a password, the usage page; everything else gets a 404.
3. **Its setup is an interactive wizard.** On every boot, `bootstrap.py` configures the gateway from your variables instead: provider connections, one model alias per entry in `EXP_MODELS` (with the capabilities, output limit and prices from Experiential's own model catalog, the same source the wizard uses), a `default` identity, and its first key.
4. **There is no official image.** The Dockerfile installs the pinned PyPI release and its compiled gateway engine.
5. **It sends anonymous telemetry by default.** The image turns it off.

## Variables

| Variable | Default | What it does |
|---|---|---|
| `OPENAI_API_KEY` | | OpenAI key. Set at least one provider. |
| `ANTHROPIC_API_KEY` | | Anthropic key. |
| `GEMINI_API_KEY` | | Google Gemini key. |
| `OPENROUTER_API_KEY` | | OpenRouter key (hundreds of models with one key). |
| `EXP_ENDPOINT_BASE_URL` | | Any OpenAI-compatible server (vLLM, Ollama, LM Studio, another Railway service), e.g. `http://ollama.railway.internal:11434/v1`. |
| `EXP_ENDPOINT_API_KEY` | | Its key. Required by Experiential: any value if the server has no auth. |
| `EXP_MODELS` | `openai:gpt-5.6-terra,anthropic:claude-sonnet-5-5,gemini:gemini-3.6-flash` | The models to expose, as `provider:model` or `alias=provider:model`, comma-separated. Providers: `openai`, `anthropic`, `gemini`, `openrouter`, `endpoint`. Entries whose provider has no key are skipped. |
| `EXP_ADMIN_PASSWORD` | generated | Password for the organization-wide usage page (user `admin`). |
| `EXP_CAPTURE_TRAFFIC` | `false` | `true` stores request and response contents in the volume, which Experiential's optimizer can learn from. Off by default for privacy. |
| `EXP_TELEMETRY` | `0` | `1` sends Experiential's anonymous product telemetry. |
| `EXP_ENDPOINT_STREAMS_TOOL_ARGUMENTS` | `true` | Whether your custom server streams tool-call arguments; the gateway needs it to allow tool calls on that server. |

Provider keys stay in Railway's variables: Experiential stores only the variable *name*. Change a key and redeploy to rotate it.

Deployed without any provider key? Experiential cannot start without a model, so the service shows a setup page instead: the health check passes and every API call answers `503 setup_required` with what to set. Add a key in **Variables** and redeploy.

## After deploying

1. Open the service's **Deployments > Deploy Logs**. The first boot prints your gateway key once:

   ```
   EXP_GATEWAY_KEY=exp_vk_...
   ```

   It is also saved in the volume: open a shell with `railway ssh` and run `cat /data/initial-key.txt`.
2. Copy the public domain from **Settings > Networking**.
3. Check it:

```sh
curl https://YOUR-DOMAIN.up.railway.app/health/ready
curl -H "Authorization: Bearer YOUR_KEY" https://YOUR-DOMAIN.up.railway.app/v1/models
```

Or run the smoke test from a clone of this repository. Pass a model alias as the last argument to also make one real call (a few tokens):

```sh
sh scripts/smoke-test.sh https://YOUR-DOMAIN.up.railway.app YOUR_KEY YOUR_ADMIN_PASSWORD gpt-5.6-terra
```

## Using it

Point any OpenAI SDK at `https://YOUR-DOMAIN.up.railway.app/v1` and use an alias from `EXP_MODELS` as the model name:

```python
from openai import OpenAI

client = OpenAI(base_url="https://YOUR-DOMAIN.up.railway.app/v1", api_key="YOUR_KEY")
reply = client.chat.completions.create(
    model="claude-sonnet-5-5",
    messages=[{"role": "user", "content": "Hello"}],
)
```

The Responses API (`/v1/responses`) and the Anthropic Messages API (`/v1/messages`, base URL `https://YOUR-DOMAIN.up.railway.app`) work the same way. From another service in the same Railway project, use the private network:

```
OPENAI_BASE_URL=http://${{experiential.RAILWAY_PRIVATE_DOMAIN}}:${{experiential.PORT}}/v1
OPENAI_API_KEY=<a gateway key>
```

**See spend:** open `https://YOUR-DOMAIN.up.railway.app/usage` in a browser (user `admin`, password `EXP_ADMIN_PASSWORD`) for every identity, or call `/usage.json` with a gateway key to see that key's own usage.

## Keys, identities and spending caps

Experiential gives each user or agent its own identity, keys, model grants and monthly budget. Manage them from a shell in the running service (`railway ssh`), where `exp` already points at the gateway's data:

```sh
exp config gateway identity create agent-1 --display-name "Support agent"
exp config gateway grant add agent-1 claude-sonnet-5-5
exp config gateway key issue agent-1 --key-id agent-1-key          # prints the key once
exp config gateway budget set --period 2026-11 --scope identity \
  --identity agent-1 --limit-nano-usd 20000000000                   # $20 for November
exp config gateway budget remaining --period 2026-11
exp config gateway key revoke agent-1-key
```

Changes apply immediately, without a redeploy. Budgets are monthly and need one `budget set` per month. Costs are estimated from Experiential's catalog prices; models outside its catalog are counted as unknown cost.

## Good to know

- **Tool calls.** Experiential allows them on OpenAI, Anthropic, OpenRouter and custom servers. It does not support them on Gemini yet, so Gemini aliases serve plain chat.
- **Models outside Experiential's catalog** (for example a model released after the pinned version) still work: the template assumes tool and JSON support and a 16,384-token output cap, and logs a warning. Prices are unknown, so spending caps cannot count them.
- **Removing a model from `EXP_MODELS` does not delete its alias.** Disable it with `exp config gateway alias disable ALIAS`.
- **Upgrades.** A weekly GitHub Action (`.github/workflows/bump-experiential.yml`) opens a pull request when Experiential publishes a new release. One-time setup: Settings > Actions > General > Workflow permissions > tick "Allow GitHub Actions to create and approve pull requests". Deploy the branch once and run the smoke test before merging.
- **One replica.** The gateway keeps its state in SQLite on the volume, so run a single replica.

## Licenses

This template is MIT-licensed. Experiential and Caddy are Apache-2.0 and run unmodified; see `NOTICE`.
