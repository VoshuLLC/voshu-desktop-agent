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

"""MCP Tool: mobile_expand_fleet.

Thin wrapper over ``artemis.fleet.provisioner.provision``. The admin
console's one-device drawer calls ``POST /api/fleet/provision`` with the
same ``count`` and reads the same response fields.
"""

from mcp_server.base import mcp
from artemis.fleet.provisioner import ensure_golden, provision

_MIN_COUNT = 1
_MAX_COUNT = 8


@mcp.tool()
async def mobile_expand_fleet(count: int = 1) -> dict:
    """Expand the fleet by provisioning golden-image clones.

    Clones the golden AVD and boots each clone. The one-device screen adds
    a single device (``count`` 1). Same bounds and response as
    ``POST /api/fleet/provision``.

    Args:
        count: How many devices to add. Integer from 1 to 8. Default 1.
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError("count must be 1..8")
    if count < _MIN_COUNT or count > _MAX_COUNT:
        raise ValueError("count must be 1..8")
    gold = await ensure_golden()
    if not gold.get("exists"):
        return {
            "ok": False,
            "error": f"golden AVD unavailable: {gold.get('error')}",
            "created_golden": gold.get("created"),
        }
    results = await provision(count)
    return {
        "ok": all(r.get("ok") for r in results),
        "results": results,
        "created_golden": gold.get("created", False),
    }
