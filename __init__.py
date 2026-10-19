"""Smart glasses as a first-class Hermes gateway platform."""
from dataclasses import fields
import importlib.util

from gateway.platform_registry import PlatformEntry
from gateway.platforms._shared import extra_or_secret
from .adapter import GlassesAdapter
from . import tools


def configured(config=None):
    extra = getattr(config, 'extra', {}) or {}
    return bool(extra_or_secret(extra, 'devices', 'GLASSES_DEVICES') or
                extra_or_secret(extra, 'token', 'GLASSES_TOKEN'))


def env_enablement():
    return {} if configured() else None


def register(ctx):
    extra = dict(
        emoji='👓', platform_hint='Replies are spoken or shown on a tiny lens: use 1–3 short sentences, no markdown, no tables, no raw URLs unless asked.',
        cron_deliver_env_var='GLASSES_HOME_CHANNEL', allowed_users_env='GLASSES_ALLOWED_USERS',
        allow_all_env='GLASSES_ALLOW_ALL_USERS', is_connected=configured,
        env_enablement_fn=env_enablement, max_message_length=4000)
    supported = {f.name for f in fields(PlatformEntry)}
    ctx.register_platform(
        name='glasses', label='Smart glasses', adapter_factory=GlassesAdapter,
        check_fn=lambda: importlib.util.find_spec('aiohttp') is not None,
        validate_config=configured, install_hint="Requires aiohttp (Hermes 'messaging' extra).",
        **{k: v for k, v in extra.items() if k in supported})
    ctx.register_tool(name='glasses_capture_photo', toolset='glasses', schema=tools.SCHEMA,
                      handler=tools.capture_photo, is_async=True, check_fn=tools.available,
                      description=tools.DESCRIPTION, emoji='👓')
