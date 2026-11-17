from __future__ import annotations

import argparse
import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Select, Static

from app.config import Settings
from app.services.audit import AuditLogger
from app.services.kube import KubeService


class ConfirmDialog(ModalScreen[bool]):
    """Small confirmation modal for access and policy changes."""

    CSS = """
    ConfirmDialog { align: center middle; }
    #confirm-box { width: 64; height: auto; padding: 1 2; border: round $warning; background: $surface; }
    #confirm-buttons { height: auto; margin-top: 1; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static("Confirm operation", markup=False)
            yield Static(self.message, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Confirm", id="confirm-yes", variant="error")
                yield Button("Cancel", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


class AICHPCManager(App):
    TITLE = "AIC HPC Manager"
    SUB_TITLE = "Kubernetes users, namespaces and storage policies"
    CSS = """
    Screen { layout: vertical; }
    #topline { height: 3; padding: 1 2; background: $boost; }
    #body { height: 1fr; }
    #sidebar { width: 30; border: round $primary; padding: 1; }
    #content { width: 1fr; border: round $primary; padding: 1 2; }
    .field-label { margin-top: 1; color: $text-muted; }
    .row { height: auto; margin-top: 1; }
    DataTable { height: 1fr; margin-top: 1; }
    Input, Select { width: 1fr; }
    Button { margin-right: 1; }
    #message { height: 3; padding: 1; color: $text; }
    #help { height: auto; color: $text-muted; }
    """
    BINDINGS = [("q", "quit", "Quit"), ("r", "refresh", "Refresh")]

    def __init__(self, settings: Settings, kube: KubeService) -> None:
        super().__init__()
        self.settings = settings
        self.kube = kube
        self.section = "Dashboard"
        self.current_namespace = ""
        self.table_rows: list[tuple[str, ...]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("AIC HPC MANAGER  |  Connected to Kubernetes", id="topline")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("MANAGEMENT")
                for label in ["Dashboard", "Users", "Namespaces", "StorageClasses", "Storage Permissions", "Persistent Volumes"]:
                    yield Button(label, id=f"nav-{label.lower().replace(' ', '-').replace('_', '-')}", variant="primary" if label == "Dashboard" else "default")
                yield Static("\nActions are audited.\nOnly hpc-manager-owned\nRoleBindings are changed.", id="help")
            with Vertical(id="content"):
                yield Static("Dashboard", id="heading")
                yield Static("", id="message")
                yield Input(placeholder="Search current view...", id="search")
                yield DataTable(id="table")
                with VerticalScroll(id="controls"):
                    yield Label("Namespace")
                    yield Select([], prompt="Select namespace", id="namespace-select")
                    yield Label("Username (Kubernetes OIDC username)", classes="field-label")
                    yield Input(placeholder="e.g. jonathan", id="username")
                    yield Label("Additional namespaces (comma-separated; leave blank to use the selected namespace)", classes="field-label")
                    yield Input(placeholder="team-a,team-b,team-c", id="assign-namespaces")
                    yield Label("Role", classes="field-label")
                    yield Select([( "User", "user"), ("Project admin", "admin")], value="user", id="role-select")
                    with Horizontal(classes="row"):
                        yield Button("Assign user", id="assign-user", variant="success")
                        yield Button("Remove selected user", id="remove-user", variant="error")
                    yield Label("Allowed StorageClasses (comma-separated)", classes="field-label")
                    yield Input(placeholder="nfs-team-a,nfs-shared", id="storage-classes")
                    with Horizontal(classes="row"):
                        yield Button("Save storage permissions", id="save-storage", variant="success")
                        yield Button("Load namespace permissions", id="load-storage")
        yield Footer()

    async def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.cursor_type = "row"
        await self.refresh_all()
        self.show_dashboard()
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""

        self.run_worker(
         self.handle_button_action(button_id),
         group="button-actions",
         exclusive=False,
        )

    async def handle_button_action(self, button_id: str) -> None:
        if button_id.startswith("nav-"):
            name = button_id[4:].replace("-", " ").title()
  
            if name == "Storageclasses":
                name = "StorageClasses"
  
            self.show_section(name)

        elif button_id == "assign-user":
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    "Grant this Kubernetes user the selected namespace role?"
                )
            )

            if confirmed:
                await self.assign_user()

        elif button_id == "remove-user":
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    "Remove the selected user's AIC HPC Manager RoleBinding "
                    "from the selected namespace?"
                )
            )

            if confirmed:
                await self.remove_selected_user()

        elif button_id == "save-storage":
            confirmed = await self.push_screen_wait(
                ConfirmDialog(
                    "Apply the edited allowed-StorageClass list "
                    "to the selected namespace?"
                )
            )

            if confirmed:
                await self.save_storage()

        elif button_id == "load-storage":
            await self.load_storage()


    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "namespace-select" and event.value is not Select.BLANK:
            self.current_namespace = str(event.value)

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.apply_search()

    async def refresh_all(self) -> None:
        namespaces = self.kube.namespaces()
        options = [(item["name"], item["name"]) for item in namespaces]
        select = self.query_one("#namespace-select", Select)
        select.set_options(options)
        if options and not self.current_namespace:
            self.current_namespace = options[0][1]
            select.value = self.current_namespace

    def set_message(self, message: str, error: bool = False) -> None:
        widget = self.query_one("#message", Static)
        widget.update(("ERROR: " if error else "") + message)

    def reset_table(self, columns: list[str]) -> DataTable:
        table = self.query_one("#table", DataTable)
        table.clear(columns=True)
        table.add_columns(*columns)
        self.table_rows = []
        return table

    def add_table_row(self, *values: object) -> None:
        self.table_rows.append(tuple(str(value) for value in values))
        self.apply_search()

    def apply_search(self) -> None:
        """Rebuild the current table using a case-insensitive substring filter."""
        try:
            table = self.query_one("#table", DataTable)
            query = self.query_one("#search", Input).value.strip().casefold()
        except Exception:
            return
        table.clear()
        for row in self.table_rows:
            if not query or any(query in value.casefold() for value in row):
                table.add_row(*row)

    def show_dashboard(self) -> None:
        self.section = "Dashboard"
        self.query_one("#heading", Static).update("Dashboard")
        namespaces = self.kube.namespaces()
        users = self.kube.all_users()
        classes = self.kube.storage_classes()
        table = self.reset_table(["Metric", "Count"])
        for row in [["Managed namespaces", str(len(namespaces))], ["Users with application-managed access", str(len(users))], ["StorageClasses", str(len(classes))]]:
            self.add_table_row(*row)
        self.set_message("Cluster inventory loaded. Use the navigation buttons to manage access and storage.")

    def show_section(self, section: str) -> None:
        self.section = section
        self.query_one("#heading", Static).update(section)
        if section == "Dashboard":
            self.show_dashboard()
        elif section == "Users":
            table = self.reset_table(["Username", "Namespace", "Role", "Binding owner"])
            for ns in self.kube.namespaces():
                for assignment in self.kube.users_for_namespace(ns["name"]):
                    self.add_table_row(assignment["username"], ns["name"], assignment["role"], assignment["owner"])
            self.set_message("Select a row to remove that user's assignment from its namespace. Slurm-owned bindings are visible but read-only.")
        elif section == "Namespaces":
            table = self.reset_table(["Namespace", "Slurm account", "Status", "Allowed StorageClasses"])
            for ns in self.kube.namespaces():
                self.add_table_row(ns["name"], ns["account"], ns["status"], ", ".join(ns["storage_classes"]) or "(not configured)")
            self.set_message("Namespace inventory filtered by namespace_filter_label in config.yaml.")
        elif section == "StorageClasses":
            table = self.reset_table(["Name", "Provisioner", "Reclaim", "Binding", "Expansion"])
            for sc in self.kube.storage_classes():
                self.add_table_row(sc["name"], sc["provisioner"], sc["reclaim_policy"], sc["binding_mode"], str(sc["allow_expansion"]))
            self.set_message("StorageClasses are read from the Kubernetes Storage API.")
        elif section == "Storage Permissions":
            table = self.reset_table(["Namespace", "Allowed StorageClasses"])
            for ns in self.kube.namespaces():
                self.add_table_row(ns["name"], ", ".join(ns["storage_classes"]) or "(none)")
            self.set_message("Select a namespace, load its current permissions, edit the comma-separated list, then save.")
        elif section == "Persistent Volumes":
            table = self.reset_table(["PV", "Status", "PVC", "StorageClass"])
            for pv in self.kube.pv_summary():
                self.add_table_row(pv["name"], pv["status"], pv["claim"], pv["storage_class"])
            self.set_message("PersistentVolume inventory. PVC details are available in namespace context through the Kubernetes API.")

    async def assign_user(self) -> None:
        selected_namespace = self.current_namespace
        username = self.query_one("#username", Input).value.strip()
        role = str(self.query_one("#role-select", Select).value)
        raw_namespaces = self.query_one("#assign-namespaces", Input).value.strip()

        if not username:
            self.set_message("Enter a Kubernetes username.", True)
            return

        namespaces = (
            list(dict.fromkeys(item.strip() for item in raw_namespaces.split(",") if item.strip()))
            if raw_namespaces
            else ([selected_namespace] if selected_namespace else [])
        )
        if not namespaces:
            self.set_message("Select a namespace or enter one or more namespace names.", True)
            return

        available = {item["name"] for item in self.kube.namespaces()}
        unknown = sorted(set(namespaces) - available)
        if unknown:
            self.set_message(
                f"Unknown or unmanaged namespace(s): {', '.join(unknown)}. No assignments were made.",
                True,
            )
            return

        succeeded: list[str] = []
        failed: list[str] = []
        for namespace in namespaces:
            try:
                self.kube.assign_user(namespace, username, role)
                succeeded.append(namespace)
            except Exception as exc:
                failed.append(f"{namespace}: {exc}")

        self.query_one("#assign-namespaces", Input).value = ""
        self.show_section("Users")
        if failed and succeeded:
            self.set_message(
                f"Assigned {username} to {', '.join(succeeded)}. Failed: {'; '.join(failed)}",
                True,
            )
        elif failed:
            self.set_message(f"No assignments completed. {'; '.join(failed)}", True)
        else:
            self.set_message(f"Assigned {username} to {', '.join(succeeded)} as {role}.")

    async def remove_selected_user(self) -> None:
        namespace = self.current_namespace
        table = self.query_one("#table", DataTable)
        if not namespace or table.row_count == 0 or table.cursor_row is None:
            self.set_message("Select a user row in the Users table first.", True)
            return
        try:
            row = table.get_row_at(table.cursor_row)
            username = str(row[0])
            selected_namespace = str(row[1]) if self.section == "Users" else namespace
            self.kube.remove_user(selected_namespace, username)
            self.set_message(f"Removed {username} from {selected_namespace}.")
            self.show_section("Users")
        except Exception as exc:
            self.set_message(str(exc), True)

    async def load_storage(self) -> None:
        if not self.current_namespace:
            self.set_message("Select a namespace first.", True)
            return
        ns = next((x for x in self.kube.namespaces() if x["name"] == self.current_namespace), None)
        if ns is None:
            self.set_message("Namespace not found in configured inventory.", True)
            return
        self.query_one("#storage-classes", Input).value = ",".join(ns["storage_classes"])
        self.set_message(f"Loaded storage permissions for {self.current_namespace}.")

    async def save_storage(self) -> None:
        namespace = self.current_namespace
        raw = self.query_one("#storage-classes", Input).value
        classes = [item.strip() for item in raw.split(",") if item.strip()]
        if not namespace:
            self.set_message("Select a namespace first.", True)
            return
        try:
            self.kube.set_namespace_storage_classes(namespace, classes)
            self.set_message(f"Saved {len(set(classes))} allowed StorageClass(es) for {namespace}.")
            self.show_section("Storage Permissions")
        except Exception as exc:
            self.set_message(str(exc), True)

    def action_refresh(self) -> None:
        try:
            self.refresh_all_sync()
            self.show_section(self.section)
            self.set_message("Inventory refreshed.")
        except Exception as exc:
            self.set_message(str(exc), True)

    def refresh_all_sync(self) -> None:
        namespaces = self.kube.namespaces()
        select = self.query_one("#namespace-select", Select)
        select.set_options([(x["name"], x["name"]) for x in namespaces])


def main() -> None:
    parser = argparse.ArgumentParser(description="AIC HPC Manager terminal application")
    parser.add_argument("-c", "--config", default="/etc/aic-hpc-manager/config.yaml")
    args = parser.parse_args()
    settings = Settings.load(args.config)
    audit_path = settings.get("app.audit_log", "/var/log/aic-hpc-manager/audit.jsonl")
    audit = AuditLogger(audit_path, settings.get("app.operator_env", "HPC_MANAGER_OPERATOR"))
    kube = KubeService(settings, audit)
    AICHPCManager(settings, kube).run()


if __name__ == "__main__":
    main()
