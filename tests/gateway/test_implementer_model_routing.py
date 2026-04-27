"""Tests for ``GatewayRunner._resolve_implementer_agent_config`` (P7a-2).

The implementer worker constructs its ``AIAgent`` from whatever this
helper returns. The contract:

* When the optional ``gateway.implementer.models.<role>`` block is
  configured, the chosen ``provider``/``model``/``fallback_model``
  override the runner's global defaults.
* Provider credentials/base_url are pulled from
  ``hermes_cli.auth.PROVIDER_REGISTRY`` (env-var lookup) so the
  implementer doesn't ship raw secrets through config.
* When the role isn't configured (or the block is absent entirely), the
  helper falls back to the runner's global model + provider so existing
  deployments without the new config keep working.

These tests build a ``GatewayRunner`` instance via ``object.__new__``
and stub only the fields the helper touches (``self.config`` and
``self._fallback_model``). That avoids spinning up real adapters
or hitting upstream providers.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict

import pytest

from gateway.config import GatewayConfig, ImplementerConfig, ModelRouteConfig
from gateway.run import GatewayRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_runner(implementer_cfg: ImplementerConfig) -> GatewayRunner:
    """Construct a ``GatewayRunner`` shell sufficient for the helper.

    Bypasses ``__init__`` so we don't bring up the full runner: we only
    need the two attributes the resolver reads.
    """
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(implementer=implementer_cfg)
    runner._fallback_model = "global-fallback-model"
    return runner


@pytest.fixture
def stub_globals(monkeypatch):
    """Pin the module-level "global" lookups the resolver falls back on.

    Without this the helper hits the live ``~/.hermes/config.yaml``
    (machine-dependent) for global model + runtime kwargs, making tests
    flaky.
    """
    monkeypatch.setattr(
        "gateway.run._resolve_gateway_model",
        lambda *a, **kw: "global-default-model",
    )
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "global-provider",
            "base_url": "https://global.example/api",
            "api_key": "global-key",
            "api_mode": "responses",
        },
    )


@pytest.fixture
def stub_provider_registry(monkeypatch):
    """Inject a fake auth.PROVIDER_REGISTRY for predictable lookups."""
    fake_registry = {
        "gemini": SimpleNamespace(
            inference_base_url="https://generativelanguage.example/v1",
            base_url_env_var="GEMINI_BASE_URL",
            api_key_env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        ),
        "anthropic": SimpleNamespace(
            inference_base_url="https://api.anthropic.example/v1",
            base_url_env_var="ANTHROPIC_BASE_URL",
            api_key_env_vars=("ANTHROPIC_API_KEY",),
        ),
    }
    monkeypatch.setattr(
        "hermes_cli.auth.PROVIDER_REGISTRY", fake_registry, raising=False,
    )
    return fake_registry


# ---------------------------------------------------------------------------
# Frontend route → Gemini config
# ---------------------------------------------------------------------------


def test_frontend_route_returns_gemini_config(
    monkeypatch, stub_globals, stub_provider_registry,
):
    monkeypatch.setenv("GEMINI_API_KEY", "gem-test-key")
    monkeypatch.delenv("GEMINI_BASE_URL", raising=False)

    runner = _make_runner(
        ImplementerConfig(models={
            "frontend": ModelRouteConfig(
                provider="gemini",
                model="gemini-2.5-pro",
                fallback_model="claude-sonnet-4-6",
            ),
        }),
    )

    cfg = runner._resolve_implementer_agent_config("frontend")
    assert cfg["provider"] == "gemini"
    assert cfg["model"] == "gemini-2.5-pro"
    assert cfg["base_url"] == "https://generativelanguage.example/v1"
    assert cfg["api_key"] == "gem-test-key"
    assert cfg["fallback_model"] == "claude-sonnet-4-6"
    # api_mode comes from the runner's global runtime kwargs.
    assert cfg["api_mode"] == "responses"


# ---------------------------------------------------------------------------
# Backend route → Anthropic config (and env BASE_URL override)
# ---------------------------------------------------------------------------


def test_backend_route_returns_claude_config_with_env_base_url_override(
    monkeypatch, stub_globals, stub_provider_registry,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "claude-test-key")
    monkeypatch.setenv(
        "ANTHROPIC_BASE_URL", "https://env-anthropic.example/v1",
    )

    runner = _make_runner(
        ImplementerConfig(models={
            "backend": ModelRouteConfig(
                provider="anthropic",
                model="claude-opus-4-6",
                fallback_model="gemini-2.5-pro",
            ),
        }),
    )

    cfg = runner._resolve_implementer_agent_config("backend")
    assert cfg["provider"] == "anthropic"
    assert cfg["model"] == "claude-opus-4-6"
    # env var must win over the registry's hard-coded inference_base_url.
    assert cfg["base_url"] == "https://env-anthropic.example/v1"
    assert cfg["api_key"] == "claude-test-key"
    assert cfg["fallback_model"] == "gemini-2.5-pro"


# ---------------------------------------------------------------------------
# Missing role → global fallback (entire shape preserved)
# ---------------------------------------------------------------------------


def test_missing_role_config_falls_back_to_global(
    stub_globals, stub_provider_registry,
):
    runner = _make_runner(ImplementerConfig(models={}))

    cfg = runner._resolve_implementer_agent_config("frontend")
    assert cfg == {
        "model": "global-default-model",
        "provider": "global-provider",
        "base_url": "https://global.example/api",
        "api_key": "global-key",
        "api_mode": "responses",
        "fallback_model": "global-fallback-model",
    }


def test_implementer_block_absent_falls_back_to_global(
    stub_globals, stub_provider_registry,
):
    """A runner with the default empty ``ImplementerConfig`` (no models
    key set in YAML) must behave identically to a configured-but-empty
    block. Regression guard for deployments running the new code without
    updating their config."""
    runner = _make_runner(ImplementerConfig())

    cfg = runner._resolve_implementer_agent_config("backend")
    assert cfg["model"] == "global-default-model"
    assert cfg["provider"] == "global-provider"
    assert cfg["fallback_model"] == "global-fallback-model"


# ---------------------------------------------------------------------------
# Partial role config inherits the missing pieces from globals.
# ---------------------------------------------------------------------------


def test_partial_route_inherits_missing_fields_from_globals(
    stub_globals, stub_provider_registry,
):
    """Role config with model only — provider+fallback come from globals."""
    runner = _make_runner(
        ImplementerConfig(models={
            "backend": ModelRouteConfig(model="custom-model-only"),
        }),
    )

    cfg = runner._resolve_implementer_agent_config("backend")
    assert cfg["model"] == "custom-model-only"
    # No provider in route → inherits global provider.
    assert cfg["provider"] == "global-provider"
    # No fallback in route → inherits runner._fallback_model.
    assert cfg["fallback_model"] == "global-fallback-model"


# ---------------------------------------------------------------------------
# Provider unknown to PROVIDER_REGISTRY → no crash; api_key/base_url stay
# ``None`` because the role explicitly named a provider — falling back to
# the global creds would cross-pollute another vendor's secrets onto a
# different endpoint. AIAgent surfaces the missing-credential error.
# ---------------------------------------------------------------------------


def test_unknown_provider_does_not_inherit_global_credentials(
    stub_globals, stub_provider_registry,
):
    runner = _make_runner(
        ImplementerConfig(models={
            "frontend": ModelRouteConfig(
                provider="never-heard-of-this",
                model="some-model",
            ),
        }),
    )

    cfg = runner._resolve_implementer_agent_config("frontend")
    # Provider preserved verbatim — AIAgent surfaces the error if invalid.
    assert cfg["provider"] == "never-heard-of-this"
    # Credentials/base_url do NOT fall through to the runner globals
    # because the role explicitly named a provider (P7a-2 codex Important #7).
    assert cfg["api_key"] is None
    assert cfg["base_url"] is None


# ---------------------------------------------------------------------------
# Provider configured but env var unset → api_key stays ``None`` (no
# silent cross-pollution from the global creds). The helper deliberately
# doesn't crash on missing keys because AIAgent gives a more actionable
# error.
# ---------------------------------------------------------------------------


def test_provider_with_unset_env_does_not_inherit_global_api_key(
    monkeypatch, stub_globals, stub_provider_registry,
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    runner = _make_runner(
        ImplementerConfig(models={
            "frontend": ModelRouteConfig(
                provider="gemini", model="gemini-2.5-pro",
            ),
        }),
    )

    cfg = runner._resolve_implementer_agent_config("frontend")
    # Role explicitly names ``gemini`` → must NOT borrow another
    # provider's key from globals.
    assert cfg["api_key"] is None
    # Base URL still the registry's hard-coded inference URL — that's
    # the right vendor's endpoint, not a cross-pollution.
    assert cfg["base_url"] == "https://generativelanguage.example/v1"
