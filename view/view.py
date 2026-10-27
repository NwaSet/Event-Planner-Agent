"""A synchronous Rich terminal UI over the UI-independent EventController.

Technical events are rendered by model.trace, not by this view. API details may
already have been reduced to a generic orchestration error; the UI does not guess
an HTTP status from that text. Failed turns are never retried automatically.
"""

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from controller import ControllerError, EventController
from dao.json_storage import sanitize_payload
from model import trace
from services import OrchestratorResult


_COMMANDS = {
    "/back": "Return to the main menu",
    "/history": "Display saved user-visible messages",
    "/usage": "Display recorded tokens and costs for this process",
    "/help": "Show available commands",
    "/exit": "Exit the application",
}


def _text(value: str) -> Text:
    """Render untrusted content literally and reuse the existing secret sanitizer."""
    return Text(sanitize_payload(value))


class TerminalView:
    def __init__(self, controller: EventController, console: Console | None = None):
        self.controller = controller
        self.console = console if console is not None else trace.console
        self._running = False

    def show_error(self, message: str) -> None:
        self.console.print(Panel(_text(message), title="Error", border_style="red"))

    def show_notice(self, message: str) -> None:
        self.console.print(_text(message), style="yellow")

    def run(self) -> None:
        """Own all input collection; Ctrl+C and EOF end the application cleanly."""
        self._running = True
        try:
            self.console.print(Panel("Multi-agent event planning assistant", title="EVENT PLANNER AGENT", border_style="cyan"))
            while self._running:
                self._show_menu()
                try:
                    choice = Prompt.ask("Choose an option", choices=["1", "2", "3", "4", "5", "6"], console=self.console)
                    if choice == "6":
                        self._running = False
                    elif choice == "1":
                        title = Prompt.ask("Conversation title (leave blank for default)", default="", console=self.console)
                        conversation_id = self.controller.create_conversation(title.strip() or None)
                        self._conversation_loop(conversation_id)
                    else:
                        conversation_id = self._select_conversation()
                        if conversation_id is not None:
                            if choice == "2":
                                self._conversation_loop(conversation_id)
                            else:
                                self._manage_conversation(choice, conversation_id)
                except ControllerError as exc:
                    self.show_error(str(exc))
        except KeyboardInterrupt:
            self.show_notice("Interrupted. Reopen the conversation to inspect any work saved before interruption.")
        except EOFError:
            self.show_notice("Input closed.")
        finally:
            self._running = False
            self.console.print("Goodbye.", style="cyan")

    def _show_menu(self) -> None:
        menu = Table(show_header=False, box=None, padding=(0, 1))
        for number, label in enumerate(("New conversation", "Open conversation", "Rename conversation",
                                        "Archive conversation", "Delete conversation", "Exit"), 1):
            menu.add_row(str(number), label)
        self.console.print(menu)

    def _select_conversation(self) -> str | None:
        conversations = self.controller.list_conversations()
        if not conversations:
            self.show_notice("No conversations found. Create a new conversation first.")
            return None
        table = Table(title="Conversations (most recently updated first)")
        for name in ("#", "ID", "Title", "Updated (UTC)", "Status"):
            table.add_column(name, overflow="fold")
        choices = {}
        for index, conversation in enumerate(conversations, 1):
            identifier = conversation["id"]
            choices[str(index)] = identifier
            choices[identifier] = identifier
            table.add_row(str(index), _text(identifier), _text(conversation["title"]),
                          _text(conversation["updated_at"]), _text(conversation["status"]))
        self.console.print(table)
        while True:
            choice = Prompt.ask("Select a number or ID (0 to cancel)", console=self.console).strip()
            if choice == "0":
                return None
            if choice in choices:
                return choices[choice]
            self.show_notice("Select a listed conversation number or ID, or enter 0 to cancel.")

    def _manage_conversation(self, choice: str, conversation_id: str) -> None:
        if choice == "3":
            title = Prompt.ask("New title (leave blank to cancel)", default="", console=self.console).strip()
            if title:
                self.controller.rename_conversation(conversation_id, title)
                self.show_notice("Conversation renamed.")
        elif choice == "4":
            self.controller.archive_conversation(conversation_id)
            self.show_notice("Conversation archived. It remains available in the conversation list.")
        elif choice == "5":
            prompt = _text(f"Delete conversation {conversation_id} and its agent memories? Technical traces will be retained")
            if Confirm.ask(prompt, default=False, console=self.console):
                self.controller.delete_conversation(conversation_id, confirmed=True)
                self.show_notice("Conversation deleted. Technical traces retained.")
            else:
                self.show_notice("Deletion cancelled.")

    def _conversation_loop(self, conversation_id: str) -> None:
        conversation = self.controller.get_conversation(conversation_id)
        if conversation is None:
            self.show_error("Conversation not found. Select an existing conversation.")
            return
        self.console.print(Rule(_text(f"Conversation: {conversation['title']}"), style="cyan"))
        self.console.print(_text(f"ID: {conversation_id} | Status: {conversation['status']}"))
        if conversation["status"] == "archived":
            self.show_notice("You may continue this conversation; its archived status will be retained.")
        self._show_history(conversation_id)
        self.console.print("Type /help for commands.", style="dim")
        while self._running:
            message = Prompt.ask("You >", console=self.console)
            command = message.strip()
            if not command:
                self.show_notice("Enter a message or a command.")
                continue
            if command == "/back":
                return
            if command == "/exit":
                self._running = False
                return
            try:
                if command == "/help":
                    self._show_help()
                elif command == "/history":
                    self._show_history(conversation_id)
                elif command == "/usage":
                    self._show_usage(conversation_id)
                else:
                    self.console.print(Rule("Technical traces", style="dim"))
                    result = self.controller.send_message(conversation_id, message)
                    self._show_result(result)
            except ControllerError as exc:
                self.show_error(str(exc))
                if exc.code == "not_found":
                    return

    def _show_history(self, conversation_id: str) -> None:
        messages = self.controller.get_history(conversation_id)
        if not messages:
            self.show_notice("No saved messages yet.")
        for message in messages:
            if message["role"] not in {"user", "assistant"}:
                continue
            label = "User" if message["role"] == "user" else "Orchestrator"
            color = "cyan" if message["role"] == "user" else "magenta"
            self.console.print(Panel(_text(message["content"]), title=label, border_style=color))

    def _show_result(self, result: OrchestratorResult) -> None:
        colors = {"completed": "green", "needs_input": "cyan", "partial": "yellow", "error": "red"}
        self.console.print(Rule("Interaction result", style="dim"))
        self.console.print(_text(f"Status: {result.status}"), style=colors.get(result.status, "yellow"))
        if result.response:
            self.console.print(Panel(_text(result.response), title="Orchestrator", border_style="magenta"))
        else:
            self.show_notice("No validated final response is available. Review the diagnostics below.")
        if result.required_information:
            questions = Text("Please provide:\n")
            for item in result.required_information:
                questions.append(_text(f"- {item}\n"))
            self.console.print(Panel(questions, title="Information needed", border_style="cyan"))
        if result.issues:
            diagnostics = Text()
            for issue in result.issues:
                diagnostics.append(_text(f"- {issue}\n"))
            self.console.print(Panel(diagnostics, title="Diagnostics", border_style="yellow"))
        if result.partial_results and result.status in {"partial", "error"}:
            self.show_notice("Partial agent work is available in the orchestration result; it is not a validated final plan.")
        if not result.persisted:
            self.show_error("Saving was incomplete. Review /history before retrying; some messages or memories may already be saved.")
        if result.status == "error":
            self.show_notice("For AI failures, check LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, connectivity, and provider limits. Details may be available in the technical traces.")

    def _show_help(self) -> None:
        table = Table(title="Conversation commands")
        table.add_column("Command")
        table.add_column("Action")
        for command, description in _COMMANDS.items():
            table.add_row(command, description)
        self.console.print(table)

    def _show_usage(self, conversation_id: str) -> None:
        usage = self.controller.get_usage(conversation_id)
        table = Table(title="Recorded conversation usage in this process")
        for name in ("Agent", "Calls", "Input", "Output", "Known cost (USD)"):
            table.add_column(name, justify="left" if name == "Agent" else "right")
        for agent, stats in [*usage["agents"].items(), ("TOTAL", usage["total"])]:
            values = [_text(agent), str(stats["calls"])]
            for name in ("prompt_tokens", "completion_tokens", "cost"):
                value = stats["known_cost" if name == "cost" else name]
                label = "unknown" if value is None else (f"${value:.6f}" if name == "cost" else str(value))
                if value is not None and stats[f"missing_{name}_calls"]:
                    label += " (partial)"
                values.append(label)
            table.add_row(*values, style="bold" if agent == "TOTAL" else None)
        self.console.print(table)
        self.console.print(
            "Usage is kept in RAM and resets after restart. These are recorded calls from this process, "
            "not all historical conversation costs. Unknown or partial values reflect missing provider usage. "
            "Failed or interrupted requests may incur unreported charges.", style="dim",
        )
