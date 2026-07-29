from __future__ import annotations

from tests.host_fixtures import HermesHost
from tokenjuice_hermes.plugin import register


def test_register_empty_ctx_config_isolated_from_ambient_hermes_config() -> None:
    # Given: tests model an empty ctx.config without inheriting host-local Hermes files.
    host = HermesHost()
    host.config = {}

    # When: the plugin registers in the default test environment.
    register(host)

    # Then: ambient machine config cannot enable structured pruning implicitly.
    assert host.hooks == ["transform_tool_result"]
