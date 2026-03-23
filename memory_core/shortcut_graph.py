"""Keyboard shortcut mapping for heavy desktop applications.

Provides fast lookup of keyboard shortcuts by application name and action,
eliminating the need for VLM-based menu navigation for common operations.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default shortcut definitions for common applications.
# Keys are lowercase app names; values are dicts mapping action -> key combo.
_DEFAULT_SHORTCUTS: dict[str, dict[str, str]] = {
    "vscode": {
        "save": "ctrl+s",
        "undo": "ctrl+z",
        "redo": "ctrl+shift+z",
        "find": "ctrl+f",
        "replace": "ctrl+h",
        "toggle_terminal": "ctrl+`",
        "format_document": "ctrl+shift+i",
        "go_to_file": "ctrl+p",
        "command_palette": "ctrl+shift+p",
    },
    "chrome": {
        "new_tab": "ctrl+t",
        "close_tab": "ctrl+w",
        "reload": "ctrl+r",
        "dev_tools": "f12",
        "address_bar": "ctrl+l",
        "find_in_page": "ctrl+f",
    },
    "explorer": {
        "new_folder": "ctrl+shift+n",
        "rename": "f2",
        "delete": "delete",
        "copy": "ctrl+c",
        "paste": "ctrl+v",
        "address_bar": "alt+d",
        "search": "ctrl+e",
    },
    "photoshop": {
        "save": "ctrl+s",
        "undo": "ctrl+z",
        "redo": "ctrl+shift+z",
        "new_layer": "ctrl+shift+n",
        "free_transform": "ctrl+t",
        "brush": "b",
        "eraser": "e",
        "select_all": "ctrl+a",
    },
    "notepad++": {
        "save": "ctrl+s",
        "undo": "ctrl+z",
        "redo": "ctrl+y",
        "find": "ctrl+f",
        "replace": "ctrl+h",
        "duplicate_line": "ctrl+d",
        "go_to_line": "ctrl+g",
        "toggle_comment": "ctrl+q",
        "close_tab": "ctrl+w",
    },
}


class ShortcutGraph:
    """Keyboard shortcut registry for desktop applications.

    Provides instant lookup of keyboard combos by app name and action,
    allowing the agent to bypass slow menu navigation for common tasks.

    Comes pre-loaded with defaults for VSCode, Chrome, Explorer, Photoshop,
    and Notepad++. Can be extended at runtime or from an external JSON file.

    Example JSON file format::

        {
            "blender": {
                "grab": "g",
                "rotate": "r",
                "scale": "s",
                "render": "f12"
            }
        }
    """

    def __init__(self, extra_json_path: Optional[str] = None):
        """Initialize the shortcut graph.

        Args:
            extra_json_path: Optional path to a JSON file with additional
                shortcut definitions. These are merged on top of the defaults,
                so you can override built-in shortcuts or add new apps.
        """
        # Deep-copy defaults so mutations don't affect the module-level dict
        self._shortcuts: dict[str, dict[str, str]] = {
            app: dict(actions) for app, actions in _DEFAULT_SHORTCUTS.items()
        }

        if extra_json_path:
            self._load_json(extra_json_path)

    def _load_json(self, path: str) -> None:
        """Merge shortcuts from an external JSON file."""
        json_path = Path(path)
        if not json_path.exists():
            logger.warning(f"Shortcut JSON not found: {path}")
            return

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.error(f"Shortcut JSON root must be a dict, got {type(data).__name__}")
                return

            for app_name, actions in data.items():
                app_key = app_name.lower().strip()
                if not isinstance(actions, dict):
                    logger.warning(f"Skipping non-dict entry for app '{app_name}'")
                    continue

                if app_key not in self._shortcuts:
                    self._shortcuts[app_key] = {}
                self._shortcuts[app_key].update(
                    {k.lower().strip(): v for k, v in actions.items() if isinstance(v, str)}
                )

            logger.info(f"Loaded shortcuts from {path} ({len(data)} apps)")

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse shortcut JSON: {e}")
        except Exception as e:
            logger.error(f"Failed to load shortcut JSON: {e}")

    def get_shortcut(self, app_name: str, action: str) -> Optional[str]:
        """Look up a keyboard shortcut for a given app and action.

        Args:
            app_name: Application name (case-insensitive), e.g. ``"VSCode"``.
            action: Action name (case-insensitive), e.g. ``"save"``.

        Returns:
            Key combo string like ``"ctrl+s"``, or ``None`` if not found.
        """
        app_key = app_name.lower().strip()
        action_key = action.lower().strip()
        app_shortcuts = self._shortcuts.get(app_key)
        if app_shortcuts is None:
            return None
        return app_shortcuts.get(action_key)

    def list_apps(self) -> list[str]:
        """Return a sorted list of all registered application names."""
        return sorted(self._shortcuts.keys())

    def list_shortcuts(self, app_name: str) -> dict[str, str]:
        """Return all shortcuts for a given application.

        Args:
            app_name: Application name (case-insensitive).

        Returns:
            Dict mapping action names to key combo strings.
            Returns an empty dict if the app is not registered.
        """
        app_key = app_name.lower().strip()
        return dict(self._shortcuts.get(app_key, {}))

    def register(self, app_name: str, action: str, shortcut: str) -> None:
        """Register or update a single shortcut at runtime.

        Args:
            app_name: Application name (case-insensitive).
            action: Action name (case-insensitive).
            shortcut: Key combo string, e.g. ``"ctrl+shift+p"``.
        """
        app_key = app_name.lower().strip()
        action_key = action.lower().strip()
        if app_key not in self._shortcuts:
            self._shortcuts[app_key] = {}
        self._shortcuts[app_key][action_key] = shortcut

    def register_app(self, app_name: str, shortcuts: dict[str, str]) -> None:
        """Register or replace all shortcuts for an application.

        Args:
            app_name: Application name (case-insensitive).
            shortcuts: Dict mapping action names to key combo strings.
        """
        app_key = app_name.lower().strip()
        self._shortcuts[app_key] = {
            k.lower().strip(): v for k, v in shortcuts.items()
        }

    def export_json(self, path: str) -> None:
        """Export the current shortcut database to a JSON file.

        Args:
            path: Destination file path.
        """
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._shortcuts, f, indent=2, ensure_ascii=False)
        logger.info(f"Exported shortcuts to {path}")

    def __repr__(self) -> str:
        total = sum(len(v) for v in self._shortcuts.values())
        return f"<ShortcutGraph apps={len(self._shortcuts)} shortcuts={total}>"
