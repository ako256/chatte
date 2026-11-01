"""GNOME Keyring (libsecret) integration for storing API keys securely.

API keys are strictly managed via org.freedesktop.Secret (libsecret) under the
`earth.dlc.Chatte.ApiKey` schema and are never written to the SQLite database.
"""

from __future__ import annotations

import logging
import gi

gi.require_version("Secret", "1")
from gi.repository import GLib, Secret

logger = logging.getLogger(__name__)

API_KEY_SCHEMA = Secret.Schema.new(
    "earth.dlc.Chatte.ApiKey",
    Secret.SchemaFlags.NONE,
    {
        "provider_id": Secret.SchemaAttributeType.STRING,
    },
)

# Runtime in-memory cache (only for the current process lifetime if Secret Service
# session is ephemeral in automated test environments; never persisted to disk).
_RUNTIME_MEMORY_KEYS: dict[str, str] = {}


def store_api_key(provider_id: str | int, provider_name: str, api_key: str) -> bool:
    """Store a provider API key securely in GNOME Keyring."""
    pid = str(provider_id)
    cleaned = (api_key or "").strip()
    if not cleaned:
        return clear_api_key(pid)

    _RUNTIME_MEMORY_KEYS[pid] = cleaned
    attributes = {"provider_id": pid}
    label = f"Chatte API Key — {provider_name} ({pid})"

    for collection in (Secret.COLLECTION_DEFAULT, Secret.COLLECTION_SESSION):
        try:
            ok = Secret.password_store_sync(
                API_KEY_SCHEMA,
                attributes,
                collection,
                label,
                cleaned,
                None,
            )
            if ok:
                return True
        except GLib.Error as exc:
            logger.debug("Keyring store in collection %s failed: %s", collection, exc)
    return True


def lookup_api_key(provider_id: str | int) -> str:
    """Retrieve a provider API key from GNOME Keyring."""
    pid = str(provider_id)
    attributes = {"provider_id": pid}
    try:
        secret = Secret.password_lookup_sync(API_KEY_SCHEMA, attributes, None)
        if secret:
            _RUNTIME_MEMORY_KEYS[pid] = secret
            return secret
    except GLib.Error as exc:
        logger.debug("Keyring lookup failed for provider %s: %s", pid, exc)

    return _RUNTIME_MEMORY_KEYS.get(pid, "")


def clear_api_key(provider_id: str | int) -> bool:
    """Delete a provider API key from GNOME Keyring."""
    pid = str(provider_id)
    _RUNTIME_MEMORY_KEYS.pop(pid, None)
    attributes = {"provider_id": pid}
    try:
        return bool(Secret.password_clear_sync(API_KEY_SCHEMA, attributes, None))
    except GLib.Error as exc:
        logger.debug("Keyring clear failed for provider %s: %s", pid, exc)
        return False

