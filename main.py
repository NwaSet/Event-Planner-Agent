"""Composition root for the Rich terminal application.

Importing this module does not start the UI, initialize an LLM client, or access
conversation files. The orchestrator constructs the existing DAO dependencies;
the controller reuses them. Runtime trace-listener failures remain governed by
model.trace: they emit a diagnostic but do not stop event delivery.
"""

from pathlib import Path

from rich.text import Text

from controller import EventController
from dao.json_storage import StorageError
from model import trace
from services import EventOrchestrator
from view import TerminalView


def main(directory: str | Path | None = None) -> int:
    """Start one terminal session; directory is optional trusted app configuration."""

    trace_dao = None
    registered = False

    try:
        orchestrator = EventOrchestrator(directory=directory)
        controller = EventController(orchestrator)
        terminal = TerminalView(controller, console=trace.console)
        trace_dao = orchestrator.trace_dao
        trace.enable_console()

        try:
            trace_dao.register_listener()
            registered = True

        except (StorageError, OSError, ValueError):
            terminal.show_error("Technical trace persistence is unavailable. Check logs.json and file access. Live terminal traces remain enabled.")
        terminal.run()
        return 0
    
    except (KeyboardInterrupt, EOFError):
        trace.console.print(Text("Application stopped."))
        return 0
    
    except (StorageError, OSError, ValueError):
        trace.console.print(Text("Application startup failed. Check storage paths, JSON format, and file access."), style="red")
        return 1
    
    finally:
        if registered and trace_dao is not None:
            trace_dao.unregister_listener()


if __name__ == "__main__":
    raise SystemExit(main())
