# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Fleet expansion tool: count limit and provisioner call."""

from unittest.mock import AsyncMock, patch

import pytest

from mcp_server.tools import mobile_expand_fleet


@pytest.mark.asyncio
async def test_expand_fleet_rejects_count_outside_1_to_8():
    with (
        patch("mcp_server.tools.fleet_expand.provision", new_callable=AsyncMock) as provision,
        patch(
            "mcp_server.tools.fleet_expand.ensure_golden", new_callable=AsyncMock
        ) as ensure_golden,
    ):
        with pytest.raises(ValueError, match="1..8"):
            await mobile_expand_fleet(count=0)
        with pytest.raises(ValueError, match="1..8"):
            await mobile_expand_fleet(count=9)
    provision.assert_not_called()
    ensure_golden.assert_not_called()


@pytest.mark.asyncio
async def test_expand_fleet_count_1_calls_provisioner():
    cloned = {"ok": True, "id": "voshu-emu-01", "serial": "emulator-5554"}
    with (
        patch("mcp_server.tools.fleet_expand.provision", new_callable=AsyncMock) as provision,
        patch(
            "mcp_server.tools.fleet_expand.ensure_golden", new_callable=AsyncMock
        ) as ensure_golden,
    ):
        ensure_golden.return_value = {"exists": True, "created": False, "rev": "rev-test"}
        provision.return_value = [cloned]
        result = await mobile_expand_fleet(count=1)

    provision.assert_awaited_once_with(1)
    ensure_golden.assert_awaited_once()
    assert result["ok"] is True
    assert result["created_golden"] is False
    assert result["results"] == [cloned]
    assert result["results"][0]["id"] == "voshu-emu-01"
    assert result["results"][0]["serial"] == "emulator-5554"


@pytest.mark.asyncio
async def test_expand_fleet_returns_provisioner_error():
    failed = {"ok": False, "id": "voshu-emu-02", "error": "boot timeout"}
    with (
        patch("mcp_server.tools.fleet_expand.provision", new_callable=AsyncMock) as provision,
        patch(
            "mcp_server.tools.fleet_expand.ensure_golden", new_callable=AsyncMock
        ) as ensure_golden,
    ):
        ensure_golden.return_value = {"exists": True, "created": True}
        provision.return_value = [failed]
        result = await mobile_expand_fleet(count=1)

    provision.assert_awaited_once_with(1)
    assert result["ok"] is False
    assert result["created_golden"] is True
    assert result["results"][0]["error"] == "boot timeout"
