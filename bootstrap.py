"""Configure the Experiential gateway from Railway variables, on every boot.

Experiential's first-run setup is an interactive wizard, which a Railway deploy
cannot answer. This script does the same work without prompts, and it is safe
to run on every boot: it only changes what the variables ask for.

  1. Initialize the gateway state in the volume (first boot only).
  2. Add a provider connection for each provider key that is set. Experiential
     stores the variable NAME, never the key itself.
  3. Create one model alias per entry in EXP_MODELS, with the capabilities,
     output limit and prices Experiential's own catalog knows for that model
     (the same source its setup wizard uses).
  4. Create the "default" identity, grant it every alias from EXP_MODELS, and
     issue its first virtual key once, printed in the deploy logs.

It never deletes or disables anything. Aliases, identities, keys and budgets
you add later with `exp config gateway ...` are left alone.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from exp.common.models import (
    BillingSource,
    ConnectionConfig,
    DiscoveredModel,
    GatewayDeploymentCapabilities,
    GatewayTokenPrices,
    read_model_catalog_document,
    resolve_discovered_model,
)
from exp.runtime.gateway.catalog_authority import (
    authored_snapshot_path,
    upsert_singleton_deployment,
)
from exp.runtime.gateway.management import GatewayManagement
from exp.runtime.gateway.provider_certification import (
    ProviderCapability,
    provider_has_certified_capability,
)
from exp.runtime.models.providers.listing import (
    HttpProviderModelLister,
    ProviderEndpoint,
    ProviderListingError,
)

DEFAULT_IDENTITY = "default"
# Exit code telling the entrypoint to serve the setup page instead of the gateway:
# Experiential refuses to start without at least one usable model.
SETUP_REQUIRED = 3
# Applied only to models missing from Experiential's catalog, so that clients
# which never send max_tokens (most SDKs) are not refused.
UNKNOWN_MODEL_MAX_OUTPUT_TOKENS = 16_384
# Providers whose current chat models all accept images, used for catalog models.
IMAGE_PROVIDERS = frozenset({"openai", "anthropic", "gemini"})
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


@dataclass(frozen=True)
class Provider:
    """One provider family the template can connect from a Railway variable."""

    connection: str
    kind: str
    key_env: str
    base_url_env: str | None = None
    requires_base_url: bool = False


PROVIDERS = (
    # Native providers always use their official endpoint (an Experiential rule).
    Provider("openai", "openai", "OPENAI_API_KEY"),
    Provider("anthropic", "anthropic", "ANTHROPIC_API_KEY"),
    Provider("gemini", "gemini", "GEMINI_API_KEY"),
    Provider("openrouter", "openrouter", "OPENROUTER_API_KEY"),
    # Any OpenAI-compatible server: vLLM, Ollama, LM Studio, a Railway service...
    Provider(
        "endpoint",
        "openai-compatible",
        "EXP_ENDPOINT_API_KEY",
        "EXP_ENDPOINT_BASE_URL",
        requires_base_url=True,
    ),
)


def log(message: str) -> None:
    print(f"exp-railway: {message}", flush=True)


def warn(message: str) -> None:
    print(f"exp-railway: WARNING: {message}", flush=True)


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


def configured_providers() -> dict[str, tuple[Provider, ConnectionConfig]]:
    """Return the providers whose variables are set, with their connection config."""
    found: dict[str, tuple[Provider, ConnectionConfig]] = {}
    for provider in PROVIDERS:
        key = env(provider.key_env)
        base_url = env(provider.base_url_env) if provider.base_url_env else ""
        if provider.requires_base_url and not base_url:
            continue
        if not key:
            if base_url:
                warn(
                    f"{provider.base_url_env} is set but {provider.key_env} is empty: Experiential "
                    f"needs a credential, so set {provider.key_env} (any value if your server has no auth)"
                )
            continue
        config = ConnectionConfig(
            provider=provider.kind,
            api_key_env=provider.key_env,
            base_url=base_url or None,
        )
        found[provider.connection] = (provider, config)
    return found


@dataclass(frozen=True)
class ModelEntry:
    alias: str
    connection: str
    model: str


def parse_models(value: str) -> list[ModelEntry]:
    """Parse EXP_MODELS: comma-separated `provider:model` or `alias=provider:model`."""
    entries: list[ModelEntry] = []
    seen: set[str] = set()
    for raw in re.split(r"[,\n]", value):
        item = raw.strip()
        if not item:
            continue
        alias, separator, target = item.partition("=")
        if not separator:
            alias, target = "", item
        connection, colon, model = target.strip().partition(":")
        connection, model, alias = connection.strip(), model.strip(), alias.strip()
        if not colon or not connection or not model:
            warn(
                f"ignoring EXP_MODELS entry {item!r}: write provider:model, e.g. openai:gpt-5.6-terra"
            )
            continue
        alias = alias or model
        if not ALIAS_PATTERN.match(alias):
            warn(f"ignoring EXP_MODELS entry {item!r}: alias {alias!r} has unsupported characters")
            continue
        if alias in seen:
            warn(f"ignoring EXP_MODELS entry {item!r}: alias {alias!r} is listed twice")
            continue
        seen.add(alias)
        entries.append(ModelEntry(alias=alias, connection=connection, model=model))
    return entries


class Listings:
    """Asks each provider once which models the key can use (cached per boot)."""

    def __init__(self) -> None:
        self._lister = HttpProviderModelLister(timeout_seconds=15.0)
        self._cache: dict[str, dict[str, DiscoveredModel] | None] = {}

    def lookup(
        self, provider: Provider, config: ConnectionConfig, model: str
    ) -> tuple[bool | None, DiscoveredModel | None]:
        """Return (listed?, provider-published metadata). listed is None when unknown."""
        if provider.connection not in self._cache:
            self._cache[provider.connection] = self._list(provider, config)
        listing = self._cache[provider.connection]
        if listing is None:
            return None, None
        found = listing.get(model)
        return (found is not None), found

    def _list(
        self, provider: Provider, config: ConnectionConfig
    ) -> dict[str, DiscoveredModel] | None:
        key = env(provider.key_env)
        if not key:
            return None
        try:
            models = self._lister.list_models(
                ProviderEndpoint(provider=config.provider, api_key=key, base_url=config.base_url)
            )
        except ProviderListingError as exc:
            warn(
                f"could not list {provider.connection} models ({exc}); using Experiential's catalog only"
            )
            return None
        except Exception as exc:  # noqa: BLE001 - a listing failure must never block boot
            warn(
                f"could not list {provider.connection} models ({type(exc).__name__}); using Experiential's catalog only"
            )
            return None
        return {item.model: item for item in models}


def streams_tool_arguments(provider: Provider) -> bool:
    """Whether the provider streams tool-call arguments (the gateway needs it for tools).

    Experiential certifies this for its native providers. For a custom
    OpenAI-compatible server its setup wizard asks the operator; here the
    variable EXP_ENDPOINT_STREAMS_TOOL_ARGUMENTS answers (default true: vLLM,
    Ollama and LM Studio all do).
    """
    if provider_has_certified_capability(provider.kind, ProviderCapability.TOOL_ARGUMENT_STREAM):
        return True
    if provider.kind == "openai-compatible":
        return env("EXP_ENDPOINT_STREAMS_TOOL_ARGUMENTS").lower() not in {"0", "false", "no", "off"}
    return False


def nano(usd_per_million: float | None) -> int | None:
    return None if usd_per_million is None else round(usd_per_million * 1_000_000_000)


def ensure_alias(
    manager: GatewayManagement,
    root: Path,
    entry: ModelEntry,
    provider: Provider,
    discovered: DiscoveredModel,
    existing_aliases: dict[str, object],
    connection_changed: bool,
) -> bool:
    """Create or refresh one direct alias. Returns True when it is usable."""
    resolved = resolve_discovered_model(discovered)
    capabilities = resolved.capabilities
    known = resolved.pricing_source.value != "unknown"
    if not known:
        warn(
            f"{entry.connection}:{entry.model} is not in Experiential's model catalog: "
            f"assuming tools and JSON output, a {UNKNOWN_MODEL_MAX_OUTPUT_TOKENS}-token output cap, "
            "and unknown prices (spend is counted as unknown cost)"
        )
        capabilities = capabilities.model_copy(
            update={
                "supports_tools": True,
                "supports_structured_output": True,
                "supports_completions": True,
                "maximum_output_tokens": capabilities.maximum_output_tokens
                or UNKNOWN_MODEL_MAX_OUTPUT_TOKENS,
            }
        )
    tools = bool(capabilities.supports_tools)
    structured = bool(capabilities.supports_structured_output)
    tool_stream = streams_tool_arguments(provider)
    gateway_capabilities = GatewayDeploymentCapabilities(
        supports_streaming=True,
        supports_streaming_tool_arguments=tool_stream,
        supports_structured_text=structured,
        supports_parallel_tool_calls=tools,
        supports_strict_tools=tools and structured and provider.kind == "openai",
        supports_image_input=known and provider.kind in IMAGE_PROVIDERS,
    )
    prices = GatewayTokenPrices(
        input_nano_usd_per_million_tokens=nano(capabilities.input_cost_per_million_tokens_usd),
        cached_input_nano_usd_per_million_tokens=nano(
            capabilities.cached_input_cost_per_million_tokens_usd
        ),
        output_nano_usd_per_million_tokens=nano(capabilities.output_cost_per_million_tokens_usd),
    )
    has_prices = prices.input_nano_usd_per_million_tokens is not None
    serving = {item.connection_id: item.config for item in manager.provider_connections()}
    normalized, snapshot, catalog_changed = upsert_singleton_deployment(
        root,
        deployment_alias=entry.alias,
        connection_name=entry.connection,
        provider_model=entry.model,
        exact_model_id=entry.model,
        revision=None,
        capabilities=capabilities,
        gateway_capabilities=gateway_capabilities,
        prices=prices,
        pricing_source=resolved.pricing_source.value if has_prices else None,
        billing_source=BillingSource.CUSTOMER_MANAGED,
        replace=True,
        serving_connections=serving,
    )
    current = existing_aliases.get(entry.alias)
    if (
        current is not None
        and getattr(current, "active", False)
        and not catalog_changed
        and not connection_changed
    ):
        log(f"model {entry.alias} -> {entry.connection}:{entry.model} (unchanged)")
        return True
    authored, _dropped = read_model_catalog_document(authored_snapshot_path(snapshot).read_bytes())
    catalog_sha256 = normalized.identity_sha256()
    revision_id = f"revision-railway-{catalog_sha256[:16]}-{datetime.now(UTC):%Y%m%d%H%M%S}"
    manager.activate_direct_alias(
        alias_id=entry.alias,
        alias_name=entry.alias,
        revision_id=revision_id,
        pool_id=entry.alias,
        snapshot_ref=f"catalog-snapshots/{snapshot.name}",
        catalog_sha256=catalog_sha256,
        provider_connections=manager.provider_bindings(authored),
    )
    if not tools:
        tool_note = "no tool calls"
    elif not tool_stream:
        tool_note = "no tool calls: Experiential does not support them for this provider yet"
    else:
        tool_note = "tool calls"
    price_note = (
        f"${capabilities.input_cost_per_million_tokens_usd:g} in / "
        f"${capabilities.output_cost_per_million_tokens_usd:g} out per million tokens"
        if has_prices
        else "prices unknown"
    )
    log(
        f"model {entry.alias} -> {entry.connection}:{entry.model} "
        f"({tool_note}, max output {capabilities.maximum_output_tokens}, {price_note})"
    )
    return True


def ensure_default_key(manager: GatewayManagement, data_dir: Path) -> None:
    """Issue the first virtual key for the default identity, once."""
    active = [key for key in manager.keys(identity_id=DEFAULT_IDENTITY) if key.active]
    key_file = data_dir / "initial-key.txt"
    if active:
        hint = f" It is also saved in {key_file}." if key_file.exists() else ""
        log(
            f"virtual key for '{DEFAULT_IDENTITY}' already issued (prefix {active[0].prefix}).{hint}"
        )
        return
    existing_ids = {key.key_id for key in manager.keys()}
    key_id = (
        "initial" if "initial" not in existing_ids else f"initial-{datetime.now(UTC):%Y%m%d%H%M%S}"
    )
    issued = manager.issue_key(identity_id=DEFAULT_IDENTITY, key_id=key_id)
    raw = issued.raw_key
    if hasattr(raw, "get_secret_value"):
        raw = raw.get_secret_value()
    previous = os.umask(0o077)
    try:
        key_file.write_text(raw + "\n", encoding="utf-8")
    finally:
        os.umask(previous)
    os.chmod(key_file, 0o600)
    bar = "=" * 72
    print(bar, flush=True)
    print("exp-railway: your gateway API key (shown once; also saved in the volume):", flush=True)
    print(f"EXP_GATEWAY_KEY={raw}", flush=True)
    print(
        "exp-railway: send it as  Authorization: Bearer <key>  to https://<your-domain>/v1",
        flush=True,
    )
    print(bar, flush=True)


def main() -> int:
    data_dir = Path(env("EXP_DATA_DIR") or "/data")
    root = Path(env("EXP_ROOT") or str(data_dir / ".exp"))
    root.mkdir(parents=True, exist_ok=True)

    manager = GatewayManagement(root)
    if not manager.initialized:
        manager.initialize(display_name=env("EXP_ORGANIZATION_NAME") or "Railway")
        log(f"initialized gateway state in {root}")

    providers = configured_providers()
    if not providers:
        print(
            "exp-railway: SETUP REQUIRED: no model provider is configured. Set at least one of "
            "OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, OPENROUTER_API_KEY, "
            "or EXP_ENDPOINT_BASE_URL with EXP_ENDPOINT_API_KEY in your service variables, "
            "then redeploy.",
            file=sys.stderr,
            flush=True,
        )
        return SETUP_REQUIRED
    changed_connections: set[str] = set()
    for name, (_provider, config) in providers.items():
        changed, _authority = manager.upsert_provider_connection(
            connection_id=name, config=config, replace=True
        )
        if changed:
            changed_connections.add(name)
        log(f"provider {name} {'configured' if changed else 'unchanged'}")

    entries = parse_models(env("EXP_MODELS"))
    if not entries:
        warn("EXP_MODELS is empty: no model alias was created from the template variables")
    listings = Listings()
    existing_aliases = {view.alias_name: view for view in manager.aliases()}
    managed: list[str] = []
    for entry in entries:
        if entry.connection not in providers:
            all_names = {provider.connection for provider in PROVIDERS}
            if entry.connection in all_names:
                log(f"skipping {entry.alias}: no key set for provider {entry.connection}")
            else:
                warn(
                    f"skipping {entry.alias}: unknown provider {entry.connection!r} "
                    f"(use one of {', '.join(sorted(all_names))})"
                )
            continue
        provider, config = providers[entry.connection]
        listed, published = listings.lookup(provider, config, entry.model)
        if listed is False:
            warn(
                f"skipping {entry.alias}: {entry.connection} does not offer model {entry.model!r} "
                "to this API key (check the spelling in EXP_MODELS)"
            )
            continue
        discovered = published or DiscoveredModel(provider=provider.kind, model=entry.model)
        if published is not None and published.provider != provider.kind:
            discovered = published.model_copy(update={"provider": provider.kind})
        try:
            if ensure_alias(
                manager,
                root,
                entry,
                provider,
                discovered,
                existing_aliases,
                connection_changed=entry.connection in changed_connections,
            ):
                managed.append(entry.alias)
        except Exception as exc:  # noqa: BLE001 - report and keep the other models
            warn(f"could not configure model {entry.alias}: {exc}")

    active_aliases = [view for view in manager.aliases() if view.active]
    if not active_aliases:
        print(
            "exp-railway: SETUP REQUIRED: the gateway has no usable model. Check EXP_MODELS and the "
            "provider keys in your service variables (see the warnings above), then redeploy.",
            file=sys.stderr,
            flush=True,
        )
        return SETUP_REQUIRED

    identities = {view.identity_id: view for view in manager.identities()}
    if DEFAULT_IDENTITY not in identities:
        manager.create_identity(identity_id=DEFAULT_IDENTITY, display_name=DEFAULT_IDENTITY)
        log(f"created identity '{DEFAULT_IDENTITY}'")
    granted = {grant.alias_name for grant in manager.grants(identity_id=DEFAULT_IDENTITY)}
    for alias in managed:
        if alias not in granted:
            manager.add_grant(identity_id=DEFAULT_IDENTITY, alias_id=alias)
            log(f"granted '{DEFAULT_IDENTITY}' access to {alias}")
    ensure_default_key(manager, data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
