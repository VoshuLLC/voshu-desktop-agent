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

"""White-label brand configuration for the console surfaces.

``BRAND.json`` lives next to the console HTML in ``apps/admin_console/`` so a
rebrand is a file edit, not a code change. Values are cached briefly so the
page can fetch per load without hammering disk.
"""

import json
import time
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(tags=["brand"])

_BRAND_PATH = Path(__file__).resolve().parent.parent / "BRAND.json"
_CACHE_TTL_SECONDS = 60.0
_DEFAULT_BRAND = {
    "name": "Voshu Device Control",
    "short": "Voshu",
    "tagline": "Device Control",
    "accent": "#3182ce",
}
_cache: dict = {"loaded_at": 0.0, "brand": dict(_DEFAULT_BRAND)}


def _load_brand() -> dict:
    """Read BRAND.json merged over defaults, cached for the TTL window."""
    now = time.monotonic()
    if now - _cache["loaded_at"] < _CACHE_TTL_SECONDS:
        return dict(_cache["brand"])
    brand = dict(_DEFAULT_BRAND)
    try:
        raw = json.loads(_BRAND_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for key in _DEFAULT_BRAND:
                value = raw.get(key)
                if isinstance(value, str) and value.strip():
                    brand[key] = value.strip()
    except (OSError, ValueError):
        pass
    _cache["loaded_at"] = now
    _cache["brand"] = brand
    return dict(brand)


@router.get("/api/brand")
async def get_brand() -> dict:
    """Return the white-label brand config for the console UI."""
    return _load_brand()
