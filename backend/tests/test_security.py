from types import SimpleNamespace

import pytest

from core.net_safety import UnsafeURL, ensure_public_url
from core.startup_checks import find_insecure_settings

GOOD = dict(
    SECRET_KEY="a" * 64, AUDIT_HMAC_KEY="b" * 64,
    DATABASE_URL="postgresql+asyncpg://meshpilot:" + "c" * 32 + "@postgres:5432/meshpilot",
    ADMIN_PASSWORD="long-enough-password", CLOUD_ROUTING_ENABLED=False,
    CLOUD_API_BASE="", CLOUD_API_KEY="", CLOUD_MODEL="",
)


def problems(**overrides):
    return find_insecure_settings(SimpleNamespace(**{**GOOD, **overrides}))


def test_strong_settings_pass():
    assert problems() == []


@pytest.mark.parametrize("overrides", [
    {"SECRET_KEY": "changeme-in-production-32chars!!"},
    {"SECRET_KEY": "short"},
    {"AUDIT_HMAC_KEY": ""},
    {"AUDIT_HMAC_KEY": "change-me-to-a-different-random-string"},
    {"DATABASE_URL": "postgresql+asyncpg://meshpilot:change-me-in-production@postgres:5432/meshpilot"},
    {"DATABASE_URL": "postgresql+asyncpg://meshpilot:changeme-in-production@postgres:5432/meshpilot"},
    {"ADMIN_PASSWORD": "short"},
    {"ADMIN_PASSWORD": "change-me-in-production"},
    {"CLOUD_ROUTING_ENABLED": True},
    {"CLOUD_ROUTING_ENABLED": True, "CLOUD_API_BASE": "http://api.example.com/v1", "CLOUD_API_KEY": "k", "CLOUD_MODEL": "m"},
])
def test_insecure_settings_are_reported(overrides):
    assert problems(**overrides)


@pytest.mark.anyio
@pytest.mark.parametrize("url", [
    "http://localhost/hook",
    "http://127.0.0.1:8000/hook",
    "http://169.254.169.254/latest/meta-data",
    "http://10.0.0.5/hook",
    "http://192.168.1.1/hook",
    "http://[::1]/hook",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "http://user:pass@example.com/hook",
])
async def test_internal_webhook_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        await ensure_public_url(url)


@pytest.fixture
def anyio_backend():
    return "asyncio"
