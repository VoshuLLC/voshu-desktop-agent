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

"""Desktop actuator: the device action contract over an HTTP agentd driver.

Most of the contract is platform-independent once the controller delegates to
the desktop driver (tap/swipe/type carry no Android semantics). Only the
actions whose Android implementation shells out to package manager or intent
machinery are overridden here: app management resolves through the agentd's
own launcher, and URL opening goes through the desktop shell instead of
``am start`` intents.
"""

import inspect

from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.action_types import ActionCode, ActionResult

__all__ = ["DesktopActuator"]


class DesktopActuator(AdbActuator):
    """AdbActuator with the Android-only actions reimplemented for desktops."""

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        """Launch/stop apps through the agentd's own name resolution.

        Android resolves user-facing names to package ids with an LLM-assisted
        inventory scan; desktop agentd launchers (Start-Process on Windows,
        `open` on macOS) already resolve names the way a user typing into a
        launcher would, so the inventory step does not exist here.
        """
        verb = action.lower()
        if verb == "launch":
            res = self.controller.launch_app(app_name)
            if inspect.iscoroutine(res):
                res = await res
            if res:
                return ActionResult.success(
                    "manage_app",
                    f"Launched '{app_name}' through the desktop launcher.",
                )
            return ActionResult.failure(
                "manage_app",
                f"Desktop launcher could not start '{app_name}'.",
                code=ActionCode.PACKAGE_NOT_FOUND,
            )
        if verb == "stop":
            res = self.controller.terminate_app(app_name)
            if inspect.iscoroutine(res):
                res = await res
            return ActionResult.success("manage_app", f"Stop requested for '{app_name}'.")
        return ActionResult.failure(
            "manage_app",
            f"Invalid manage_app action: {action}",
            code=ActionCode.INVALID_ARGS,
        )

    async def open_link(self, url: str) -> ActionResult:
        """Open a URL with the desktop's default browser, not Android intents."""
        driver = getattr(self.controller, "_driver", None)
        if driver is not None and hasattr(driver, "execute_shell"):
            try:
                # cmd's start parses the first quoted token as a window title,
                # so the empty title keeps the URL itself unquoted-safe.
                await driver.execute_shell(f'cmd /c start "" "{url}"')
                return ActionResult.success("open_link", f"Opened link '{url}'.")
            except Exception as exc:
                return ActionResult.failure("open_link", f"Failed to open link: {exc}")
        return await super().open_link(url)
