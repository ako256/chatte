"""Main Adw.Application entry point for Chatte (earth.dlc.Chatte)."""

from __future__ import annotations

from pathlib import Path
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from chatte import APP_ID, APP_NAME, APP_VERSION
from chatte.ui.window import ChatteWindow


class ChatteApplication(Adw.Application):
    """Native GNOME 50 Application for Chatte (`earth.dlc.Chatte`)."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname(APP_ID)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        self._load_css()
        self._setup_actions()

    def _load_css(self) -> None:
        css_path = Path(__file__).resolve().parent / "style.css"
        if not css_path.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css_path))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _setup_actions(self) -> None:
        new_chat_act = Gio.SimpleAction.new("new-chat", None)
        new_chat_act.connect("activate", self._on_new_chat)
        self.add_action(new_chat_act)
        self.set_accels_for_action("app.new-chat", ["<primary>n"])

        prefs_act = Gio.SimpleAction.new("preferences", None)
        prefs_act.connect("activate", self._on_preferences)
        self.add_action(prefs_act)
        self.set_accels_for_action("app.preferences", ["<primary>comma"])

        skills_act = Gio.SimpleAction.new("skills", None)
        skills_act.connect("activate", self._on_skills)
        self.add_action(skills_act)
        self.set_accels_for_action("app.skills", ["<primary><shift>s"])

        about_act = Gio.SimpleAction.new("about", None)
        about_act.connect("activate", self._on_about)
        self.add_action(about_act)

        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda *_: self.quit())
        self.add_action(quit_act)
        self.set_accels_for_action("app.quit", ["<primary>q"])

    def do_activate(self) -> None:
        win = self.props.active_window
        if not win:
            win = ChatteWindow(application=self)
        win.present()

    def _on_new_chat(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        win = self.props.active_window
        if isinstance(win, ChatteWindow):
            win.action_new_chat()

    def _on_preferences(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        win = self.props.active_window
        if isinstance(win, ChatteWindow):
            win.open_preferences_dialog()

    def _on_skills(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        win = self.props.active_window
        if isinstance(win, ChatteWindow):
            win.open_skills_dialog()

    def _on_about(self, _action: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
        about = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon=APP_ID,
            developer_name="DLC Earth",
            version=APP_VERSION,
            comments=(
                "Native GNOME 50 AI Assistant built with GTK 4.22, Libadwaita 1.9, "
                "GtkSourceView 5, PangoCairo LaTeX, and GNOME Keyring security."
            ),
            license_type=Gtk.License.GPL_3_0,
            website="https://dlc.earth",
        )
        win = self.props.active_window
        about.present(win)


def main(argv: list[str] | None = None) -> int:
    app = ChatteApplication()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())

