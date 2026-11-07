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

"""Voshu Device Control (Artemis engine) — Unified Command Line Interface (CLI)."""

from typing import Annotated

from artemis._version import __version__
from artemis.interfaces.cli.commands.batch import batch_command
from artemis.interfaces.cli.commands.doctor import doctor_command
from artemis.interfaces.cli.commands.helper import helper_app
from artemis.interfaces.cli.commands.init import init_command
from artemis.interfaces.cli.commands.mcp import mcp_command
from artemis.interfaces.cli.commands.run import run_command
from artemis.interfaces.cli.commands.server import server_app
from artemis.interfaces.cli.commands.server_lifecycle import (
    restart_command,
    status_command,
    stop_command,
)
from artemis.interfaces.cli.commands.trace import trace_app
from artemis.interfaces.cli.commands.ui import ui_command
from third_party.mobile_use.utils.logger import get_logger
from rich.console import Console
from rich.panel import Panel
import typer

logger = get_logger(__name__)

app = typer.Typer(
    name="artemis",
    help=(
        "☕ Voshu Device Control (Artemis engine): "
        "Autonomous Multimodal Android Agent & Testing Framework."
    ),
    add_completion=False,
    pretty_exceptions_enable=False,
    no_args_is_help=True,
)

# Register subcommands
app.command(name="ui", help="Launch the unified Showcase UI & Admin Console in your browser.")(
    ui_command
)
app.command(name="restart", help="Restart running Voshu Device Control UI & server.")(
    restart_command
)
app.command(name="stop", help="Stop running Voshu Device Control UI & server.")(stop_command)
app.command(name="status", help="Display Voshu Device Control UI & server status.")(
    status_command
)
app.command(name="run", help="Execute an autonomous task on a mobile device.")(run_command)
app.command(name="init", help="Interactive quickstart wizard to configure API keys & device.")(
    init_command
)
app.command(name="doctor", help="Check system prerequisites, device status, and configuration.")(
    doctor_command
)
app.command(name="batch", help="Execute a batch sequence of automation tasks.")(batch_command)
app.command(name="mcp", help="Start the Artemis Model Context Protocol (MCP) server.")(mcp_command)
app.add_typer(server_app, name="server", help="Cloud Run proxy and web dashboard server.")
app.add_typer(trace_app, name="trace", help="Inspect and query execution traces.")
app.add_typer(
    helper_app, name="helper", help="Manage the Accessibility Helper APK on attached devices."
)


def version_callback(value: bool):
    if value:
        console = Console()
        console.print(
            Panel(
                f"[bold cyan]Voshu Device Control[/bold cyan] v{__version__}\n"
                "[dim]Autonomous Multimodal Mobile AI Engine · Artemis engine[/dim]",
                title="☕ Voshu",
                expand=False,
            )
        )
        raise typer.Exit()


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Show Voshu Device Control version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
):
    """Voshu Device Control (Artemis engine) — Autonomous Mobile Agent CLI."""
    pass


def cli():
    """Main CLI entrypoint."""
    app()


if __name__ == "__main__":
    cli()
