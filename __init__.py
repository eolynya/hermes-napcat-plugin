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
    #    skill_view("hermes-napcat:qq-napcat") resolves, AND copy it into the
    #    flat ~/.hermes/skills/qq/ tree so it appears in the <available_skills>
    #    index (auto-loaded, model sees it without explicit lookup).
    #
    #    register_skill() alone is NOT enough for auto-load: its contract
    #    (hermes_cli/plugins.py:1264) explicitly says plugin skills do NOT
    #    enter the flat tree and are NOT listed in <available_skills> — they
    #    are opt-in explicit loads only.  To make the QQ skill auto-load we
    #    must ALSO copy it into ~/.hermes/skills/qq/SKILL.md (the flat tree is
    #    what get_all_skills_dirs()/agent_init scan for the index).
    #
    #    Copy is guarded: if the destination already exists (e.g. a previously
    #    installed/enhanced copy), we do NOT overwrite it — the local copy is
    #    authoritative and may be richer than the bundled 15KB upstream file.
    #    This makes the plugin's skill auto-available on a fresh install while
    #    never clobbering a user's local enhancements.
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
            # Auto-load via flat tree (skip if an existing copy is present).
            try:
                from hermes_constants import get_skills_dir

                _dst = get_skills_dir() / "qq" / "SKILL.md"
                if _dst.exists():
                    logger.info(
                        "NapCat: flat skill %s already exists — keeping local copy",
                        _dst,
                    )
                else:
                    _dst.parent.mkdir(parents=True, exist_ok=True)
                    import shutil

                    shutil.copy2(_SKILL_PATH, _dst)
                    logger.info("NapCat: installed skill → %s (auto-load via flat tree)", _dst)
            except Exception:
                logger.warning("NapCat: failed to install skill into flat tree", exc_info=True)
        else:
            logger.warning("NapCat: bundled skill not found at %s", _SKILL_PATH)
    except Exception:
        logger.warning("NapCat: failed to register bundled skill", exc_info=True)

    # 4) Aggregate the bundled qq_* toolset with the core tools so a QQ agent
    #    gets working tools (terminal/file/web/...) out of the box, not just
    #    40+ qq_* OneBot calls.  The platform default toolset for a plugin is
    #    derived as "hermes-{platform}" (= "hermes-napcat") by
    #    _get_platform_tools, and that static definition ships with
    #    includes: [] — so without this, an unconfigured platform_toolsets
    #    leaves the QQ agent tool-less beyond qq_*.
    #
    #    We re-create "hermes-napcat" at runtime via create_custom_toolset()
    #    to add hermes-cli (the 56-tool core set: terminal/file/web/memory/
    #    skills/todo/vision/clarify/delegation/code_execution/...) to its
    #    includes.  This is a process-local enhancement of the in-memory
    #    TOOLSETS table — no core source edit, survives `hermes update`,
    #    and is idempotent across plugin re-registration.
    try:
        from toolsets import TOOLSETS, create_custom_toolset

        _ts = TOOLSETS.get("hermes-napcat") or {}
        _tools = list(_ts.get("tools", []))
        _includes = list(_ts.get("includes", []))
        if "hermes-cli" not in _includes:
            _includes.append("hermes-cli")
        create_custom_toolset(
            name="hermes-napcat",
            description=_ts.get(
                "description", "QQ (NapCat / OneBot 11) toolset + core tools"
            ),
            tools=_tools,
            includes=_includes,
        )
        logger.info(
            "NapCat: aggregated hermes-napcat toolset (tools=%d includes=%s)",
            len(_tools),
            _includes,
        )
    except Exception:
        logger.warning("NapCat: failed to aggregate hermes-napcat toolset", exc_info=True)

    # 5) Re-register the qq_* tools through ctx.register_tool so the plugin
    #    manager actually SEES the "napcat" toolset.  qq_tool.py registers
    #    them module-level via tools.registry.register (toolset="napcat"),
    #    which populates the global registry but never adds the names to
    #    PluginManager._plugin_tool_names — so get_plugin_toolsets() returns
    #    nothing for this plugin, and _get_platform_tools() never enables the
    #    napcat toolset for the platform.  Result (observed on 2026-08-11):
    #    QQ sessions had ONLY core tools (terminal/file/web/...), the 48
    #    qq_* tools were registered but never loaded into the agent.
    #
    #    Re-registering through ctx.register_tool is idempotent (registry
    #    accepts the same name again) and records the names in
    #    _plugin_tool_names, making get_plugin_toolsets() return
    #    ("napcat", ...) → _get_platform_tools enables it (new plugin,
    #    default-enabled rule at tools_config.py:2430).
    try:
        from tools.registry import registry

        _qq_names = registry.get_tool_names_for_toolset("napcat")
        _registered = 0
        for _tn in _qq_names:
            _te = registry.get_entry(_tn)
            if _te is None:
                continue
            ctx.register_tool(
                name=_tn,
                toolset="napcat",
                schema=_te.schema,
                handler=_te.handler,
                is_async=bool(getattr(_te, "is_async", False)),
                description=_te.description or "",
                emoji=getattr(_te, "emoji", "") or "",
            )
            _registered += 1
        logger.info(
            "NapCat: re-registered %d qq_* tools via ctx.register_tool (toolset=napcat)",
            _registered,
        )
    except Exception:
        logger.warning("NapCat: failed to re-register qq_* tools via ctx", exc_info=True)