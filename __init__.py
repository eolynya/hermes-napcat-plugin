"""hermes-napcat: NapCat (QQ / OneBot 11) platform plugin for Hermes Agent.

Plugin entry point.  Registers the ``napcat`` gateway platform adapter and
the ``qq_*`` toolset through the public PluginContext surface
(``ctx.register_platform`` + ``ctx.register_tool``) — zero Hermes core edits.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["register"]

# Path to the bundled QQ skill shipped inside this plugin
# (hermes_napcat/skills/qq/SKILL.md).  Registered via ctx.register_skill so it
# is resolvable as skill_view("hermes-napcat:qq-napcat") without touching the
# flat ~/.hermes/skills tree.
_SKILL_PATH = Path(__file__).resolve().parent / "hermes_napcat" / "skills" / "qq" / "SKILL.md"


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

    # 3) Bundled QQ skill (hermes_napcat/skills/qq/SKILL.md) — register it so
    #    skill_view("hermes-napcat:qq-napcat") resolves.  The legacy
    #    `hermes-napcat install` path copies this file into the flat
    #    ~/.hermes/skills/qq/ tree; the plugin path registers it as a
    #    plugin-scoped read-only skill instead (no tree writes, no clobbering
    #    a locally-enhanced copy).  Registered last so a failure here never
    #    blocks platform registration.
    try:
        if _SKILL_PATH.exists():
            ctx.register_skill(
                name="qq-napcat",
                path=_SKILL_PATH,
                description=(
                    "Interact with QQ via the NapCat / OneBot 11 adapter. "
                    "Use for sending messages, group management, file "
                    "transfers, member info, notices, reactions, OCR, and "
                    "translation."
                ),
            )
            logger.info("NapCat: registered bundled skill qq-napcat")
        else:
            logger.warning("NapCat: bundled skill not found at %s", _SKILL_PATH)
    except Exception:
        logger.warning("NapCat: failed to register bundled skill", exc_info=True)