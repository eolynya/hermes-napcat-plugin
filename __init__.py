"""hermes-napcat: NapCat (QQ / OneBot 11) platform plugin for Hermes Agent.

Plugin entry point.  Registers the ``napcat`` gateway platform adapter and
the ``qq_*`` toolset through the public PluginContext surface
(``ctx.register_platform`` + ``ctx.register_tool``) — zero Hermes core edits.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

__all__ = ["register"]


def check_requirements() -> bool:
    """Passive dependency probe — is aiohttp importable right now?"""
    try:
        import aiohttp  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    """Validate that the napcat platform has an http_api configured."""
    extra = getattr(config, "extra", {}) or {}
    http_api = extra.get("http_api") or ""
    return bool(str(http_api).rstrip("/"))


def is_connected(config) -> bool:
    """Considered 'connected' when http_api is configured."""
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("http_api"))


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system at startup."""
    # 1) qq_* toolset — import triggers the module-level registry.register()
    #    calls (qq_tool.py registers ~50 tools in the "napcat" toolset).
    #    ⚠️ Must use RELATIVE import: the plugin is loaded as
    #    hermes_plugins.<slug>, and an absolute `import hermes_napcat`
    #    resolves against site-packages (the legacy pip version), not the
    #    plugin dir — _load_directory_module does NOT put plugin_dir on
    #    sys.path. (2026-08-11: fixed connect() version-shadowing bug)
    try:
        from .hermes_napcat import qq_tool  # noqa: F401
    except Exception:
        logger.warning("NapCat: failed to import qq_tool", exc_info=True)

    # 2) napcat platform adapter.  Relative import — see note above.
    try:
        from .hermes_napcat.adapter import NapCatAdapter

        ctx.register_platform(
            name="napcat",
            label="NapCat",
            adapter_factory=lambda cfg: NapCatAdapter(cfg),
            check_fn=check_requirements,
            validate_config=validate_config,
            is_connected=is_connected,
            required_env=[],
            install_hint="NapCat 容器: docker run -d --name napcat --net host ... 详见 README",
            emoji="🐧",
            allow_update_command=True,
            platform_hint=(
                "You are communicating with the user via QQ (NapCat / OneBot 11). "
                "QQ does not render Markdown — send plain text only (no tables, "
                "no **bold**, no ## headings; use - bullets and 1) numbering). "
                "Keep responses concise and friendly."
            ),
        )
    except Exception:
        logger.warning("NapCat: failed to register platform adapter", exc_info=True)