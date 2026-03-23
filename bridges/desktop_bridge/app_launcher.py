"""
OpenClaw 2.0 ACI Framework - Smart App Launcher.

Resolves application names to full executable paths on Windows, eliminating
the need for users to provide exact paths when launching apps via Win+R.

Search strategy (in order):
1. Known full paths (hardcoded likely locations for popular apps)
2. App Paths registry (HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths)
3. Common installation directories (Program Files, Program Files (x86), LocalAppData)
4. PATH environment variable
5. Start Menu shortcut (.lnk) parsing
"""

from __future__ import annotations

import glob
import logging
import os
import platform
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# Convenience: get variable references at module level.
if _IS_WINDOWS:
    _pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    _pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    _localapp = os.environ.get("LOCALAPPDATA", "")
    _appdata = os.environ.get("APPDATA", "")
else:
    _pf = r"C:\Program Files"
    _pf86 = r"C:\Program Files (x86)"
    _localapp = ""
    _appdata = ""

# Common app name -> executable mappings for popular Chinese and international apps.
# Each entry lists ALL known exe name variants for that app.
_KNOWN_APPS: dict[str, list[str]] = {
    "wechat": ["Weixin.exe", "WeChat.exe"],
    "weixin": ["Weixin.exe", "WeChat.exe"],
    "微信": ["Weixin.exe", "WeChat.exe"],
    "qq": ["QQ.exe"],
    "dingtalk": ["DingTalk.exe", "DingtalkLauncher.exe"],
    "钉钉": ["DingTalk.exe", "DingtalkLauncher.exe"],
    "feishu": ["Feishu.exe", "Lark.exe"],
    "飞书": ["Feishu.exe", "Lark.exe"],
    "lark": ["Lark.exe", "Feishu.exe"],
    "vscode": ["Code.exe"],
    "code": ["Code.exe"],
    "chrome": ["chrome.exe"],
    "firefox": ["firefox.exe"],
    "edge": ["msedge.exe"],
    "notepad": ["notepad.exe"],
    "explorer": ["explorer.exe"],
    "terminal": ["wt.exe", "WindowsTerminal.exe"],
    "cmd": ["cmd.exe"],
    "powershell": ["powershell.exe"],
    "word": ["WINWORD.EXE"],
    "excel": ["EXCEL.EXE"],
    "powerpoint": ["POWERPNT.EXE"],
    "outlook": ["OUTLOOK.EXE"],
    "teams": ["ms-teams.exe", "Teams.exe"],
    "notion": ["Notion.exe"],
    "obsidian": ["Obsidian.exe"],
    "typora": ["Typora.exe"],
    "postman": ["Postman.exe"],
    "cursor": ["Cursor.exe"],
    "clash": ["clash-verge.exe", "Clash for Windows.exe"],
    "telegram": ["Telegram.exe"],
    "discord": ["Discord.exe", "Update.exe"],
    "steam": ["steam.exe"],
    "bilibili": ["哔哩哔哩.exe", "bilibili.exe"],
    "哔哩哔哩": ["哔哩哔哩.exe", "bilibili.exe"],
    "网易云音乐": ["cloudmusic.exe"],
    "cloudmusic": ["cloudmusic.exe"],
}

# Alias groups: each list contains names that should resolve to the same app.
# This enables fuzzy matching: if one alias is a _KNOWN_APPS key, all aliases
# share its exe list.
_ALIAS_GROUPS: list[list[str]] = [
    ["wechat", "weixin", "微信"],
    ["qq", "腾讯qq"],
    ["dingtalk", "钉钉", "dingding"],
    ["feishu", "飞书", "lark"],
    ["bilibili", "哔哩哔哩", "b站"],
    ["vscode", "code", "visual studio code"],
    ["chrome", "谷歌浏览器", "google chrome"],
    ["firefox", "火狐浏览器"],
    ["edge", "微软edge"],
    ["telegram", "电报", "tg"],
    ["discord", "dc"],
    ["网易云音乐", "cloudmusic", "netease music"],
    ["steam", "蒸汽"],
    ["notion", "notion笔记"],
    ["obsidian", "黑曜石"],
]

# Build a reverse lookup: alias -> canonical key (first entry in group that
# exists in _KNOWN_APPS).
_ALIAS_MAP: dict[str, str] = {}
for _group in _ALIAS_GROUPS:
    # Find the canonical key: first alias that already has a _KNOWN_APPS entry.
    _canonical = None
    for _a in _group:
        if _a in _KNOWN_APPS:
            _canonical = _a
            break
    if _canonical:
        for _a in _group:
            _ALIAS_MAP[_a] = _canonical

# Known full paths: app name -> list of likely absolute paths.
# These are checked first and provide the fastest, most reliable resolution
# for popular apps that install to well-known locations.
_KNOWN_PATHS: dict[str, list[str]] = {}

def _build_known_paths() -> dict[str, list[str]]:
    """Build the _KNOWN_PATHS dict using current environment variables."""
    paths: dict[str, list[str]] = {}

    # --- WeChat / Weixin ---
    wechat_paths = [
        os.path.join(_pf, "Tencent", "Weixin", "Weixin.exe"),
        os.path.join(_pf86, "Tencent", "Weixin", "Weixin.exe"),
        os.path.join(_pf, "Tencent", "WeChat", "WeChat.exe"),
        os.path.join(_pf86, "Tencent", "WeChat", "WeChat.exe"),
        os.path.join(_appdata, "Tencent", "WeChat", "WeChat.exe"),
        os.path.join(_appdata, "Tencent", "Weixin", "Weixin.exe"),
    ]
    for alias in ("wechat", "weixin", "微信"):
        paths[alias] = wechat_paths

    # --- QQ ---
    qq_paths = [
        os.path.join(_pf, "Tencent", "QQNT", "QQ.exe"),
        os.path.join(_pf86, "Tencent", "QQ", "Bin", "QQ.exe"),
        os.path.join(_pf, "Tencent", "QQ", "Bin", "QQ.exe"),
        os.path.join(_pf86, "Tencent", "QQ", "QQ.exe"),
    ]
    paths["qq"] = qq_paths

    # --- DingTalk ---
    dingtalk_paths = [
        os.path.join(_pf, "DingDing", "DingTalk.exe"),
        os.path.join(_pf86, "DingDing", "DingTalk.exe"),
        os.path.join(_localapp, "DingTalk", "DingTalk.exe"),
    ]
    for alias in ("dingtalk", "钉钉"):
        paths[alias] = dingtalk_paths

    # --- Feishu / Lark ---
    feishu_paths = [
        os.path.join(_localapp, "Feishu", "Feishu.exe"),
        os.path.join(_localapp, "Lark", "Lark.exe"),
        os.path.join(_pf, "Lark", "Lark.exe"),
        os.path.join(_pf86, "Lark", "Lark.exe"),
    ]
    for alias in ("feishu", "飞书", "lark"):
        paths[alias] = feishu_paths

    # --- VS Code ---
    vscode_paths = [
        os.path.join(_localapp, "Programs", "Microsoft VS Code", "Code.exe"),
        os.path.join(_pf, "Microsoft VS Code", "Code.exe"),
        os.path.join(_pf86, "Microsoft VS Code", "Code.exe"),
    ]
    for alias in ("vscode", "code"):
        paths[alias] = vscode_paths

    # --- Chrome ---
    chrome_paths = [
        os.path.join(_pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(_pf86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(_localapp, "Google", "Chrome", "Application", "chrome.exe"),
    ]
    paths["chrome"] = chrome_paths

    # --- Firefox ---
    firefox_paths = [
        os.path.join(_pf, "Mozilla Firefox", "firefox.exe"),
        os.path.join(_pf86, "Mozilla Firefox", "firefox.exe"),
    ]
    paths["firefox"] = firefox_paths

    # --- Edge ---
    edge_paths = [
        os.path.join(_pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(_pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    paths["edge"] = edge_paths

    # --- Cursor ---
    cursor_paths = [
        os.path.join(_localapp, "Programs", "cursor", "Cursor.exe"),
        os.path.join(_localapp, "cursor", "Cursor.exe"),
    ]
    paths["cursor"] = cursor_paths

    # --- Steam ---
    steam_paths = [
        os.path.join(_pf86, "Steam", "steam.exe"),
        os.path.join(_pf, "Steam", "steam.exe"),
    ]
    paths["steam"] = steam_paths

    # --- Telegram ---
    telegram_paths = [
        os.path.join(_appdata, "Telegram Desktop", "Telegram.exe"),
        os.path.join(_pf, "Telegram Desktop", "Telegram.exe"),
    ]
    paths["telegram"] = telegram_paths

    # --- Discord ---
    discord_paths = [
        os.path.join(_localapp, "Discord", "Update.exe"),
    ]
    paths["discord"] = discord_paths

    # --- Notion ---
    notion_paths = [
        os.path.join(_localapp, "Programs", "Notion", "Notion.exe"),
        os.path.join(_localapp, "Notion", "Notion.exe"),
    ]
    paths["notion"] = notion_paths

    # --- Obsidian ---
    obsidian_paths = [
        os.path.join(_localapp, "Obsidian", "Obsidian.exe"),
    ]
    paths["obsidian"] = obsidian_paths

    return paths


if _IS_WINDOWS:
    _KNOWN_PATHS = _build_known_paths()


# Filename patterns that indicate an uninstaller rather than the real app.
_UNINSTALL_PATTERNS = ("卸载", "unins", "uninst", "uninstall", "remove")

# Common installation root directories.
_INSTALL_ROOTS: list[str] = []
if _IS_WINDOWS:
    _INSTALL_ROOTS = [_pf, _pf86, _localapp, _appdata]


def _is_uninstaller(path: str) -> bool:
    """Return True if *path* looks like an uninstaller executable."""
    lower = path.lower().replace("\\", "/")
    return any(p in lower for p in _UNINSTALL_PATTERNS)


def _resolve_alias(name_lower: str) -> str:
    """If *name_lower* is a known alias, return the canonical key.

    Otherwise return *name_lower* unchanged.
    """
    return _ALIAS_MAP.get(name_lower, name_lower)


def _fuzzy_match_known_apps(name_lower: str) -> Optional[list[str]]:
    """Try partial / substring matching against _KNOWN_APPS keys and alias groups.

    Returns the exe list if a match is found, otherwise None.
    """
    # First try alias resolution.
    canonical = _resolve_alias(name_lower)
    if canonical != name_lower and canonical in _KNOWN_APPS:
        return _KNOWN_APPS[canonical]

    # Try substring matching: does name_lower appear inside any known key or
    # vice-versa?
    for key, exes in _KNOWN_APPS.items():
        if name_lower in key or key in name_lower:
            return exes

    # Try substring matching against all aliases.
    for group in _ALIAS_GROUPS:
        for alias in group:
            if name_lower in alias or alias in name_lower:
                canonical_key = _ALIAS_MAP.get(alias)
                if canonical_key and canonical_key in _KNOWN_APPS:
                    return _KNOWN_APPS[canonical_key]

    return None


def resolve_app(name: str) -> Optional[str]:
    """Resolve an application name to its full executable path.

    Args:
        name: Application name (e.g. "WeChat", "vscode", "微信").

    Returns:
        Full path to the executable, or ``None`` if not found.
    """
    if not _IS_WINDOWS:
        logger.warning("app_launcher: not running on Windows")
        return None

    # If it's already a full path, return it directly.
    if os.path.isfile(name):
        return name

    name_lower = name.lower().strip().rstrip(".exe")

    # Step 0: Resolve aliases (e.g. "dingding" -> "dingtalk").
    canonical = _resolve_alias(name_lower)

    # Step 1: Check known full paths (fastest, most reliable).
    known = _KNOWN_PATHS.get(canonical) or _KNOWN_PATHS.get(name_lower)
    if known:
        for p in known:
            if os.path.isfile(p):
                logger.info("app_launcher: found via known path: %s", p)
                return p

    # Step 2: Check App Paths registry.
    path = _check_app_paths_registry(canonical)
    if path:
        return path

    # Step 3: Resolve known app names to executable names.
    exe_names = _KNOWN_APPS.get(canonical)
    if exe_names is None:
        exe_names = _KNOWN_APPS.get(name_lower)
    if exe_names is None:
        # Try fuzzy matching.
        exe_names = _fuzzy_match_known_apps(name_lower)
    if exe_names is None:
        exe_names = [f"{name_lower}.exe", f"{name}.exe"]

    # Step 4: Search common installation directories.
    for exe_name in exe_names:
        path = _search_install_dirs(exe_name)
        if path:
            return path

    # Step 5: Check PATH.
    for exe_name in exe_names:
        path = _check_path(exe_name)
        if path:
            return path

    # Step 6: Search Start Menu shortcuts.
    path = _search_start_menu(name_lower, exe_names)
    if path:
        return path

    logger.info("app_launcher: could not resolve '%s'", name)
    return None


def _check_app_paths_registry(name_lower: str) -> Optional[str]:
    """Check Windows App Paths registry for the application."""
    try:
        import winreg

        exe_variants = [f"{name_lower}.exe", name_lower]

        for key_path in [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
        ]:
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                # Enumerate subkeys looking for a match.
                i = 0
                while True:
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        if subkey_name.lower().rstrip(".exe") == name_lower:
                            subkey = winreg.OpenKey(key, subkey_name)
                            value, _ = winreg.QueryValueEx(subkey, "")
                            winreg.CloseKey(subkey)
                            if value and os.path.isfile(value):
                                logger.info("app_launcher: found via registry: %s", value)
                                return value
                        i += 1
                    except OSError:
                        break
                winreg.CloseKey(key)
            except OSError:
                continue
    except ImportError:
        pass
    return None


def _search_install_dirs(exe_name: str) -> Optional[str]:
    """Search common installation directories for the executable."""
    for root in _INSTALL_ROOTS:
        if not root or not os.path.isdir(root):
            continue

        # Direct child: root/AppName/exe_name
        # Or deeper: root/**/exe_name (up to 3 levels deep)
        for depth in range(1, 4):
            pattern = os.path.join(root, *["*"] * depth, exe_name)
            matches = glob.glob(pattern)
            # Filter out uninstallers.
            matches = [m for m in matches if not _is_uninstaller(m)]
            if matches:
                # Prefer the shortest path (most direct match).
                best = min(matches, key=len)
                logger.info("app_launcher: found in install dir: %s", best)
                return best

    # Special case: vendor-specific directories that may sit outside the
    # standard roots (e.g. Tencent apps under a Tencent sub-tree).
    vendor_roots = []
    if _IS_WINDOWS:
        for base in (_pf, _pf86, _appdata):
            vendor_roots.append(os.path.join(base, "Tencent"))

    for vendor_root in vendor_roots:
        if os.path.isdir(vendor_root):
            for depth in range(1, 5):
                pattern = os.path.join(vendor_root, *["*"] * depth, exe_name)
                matches = glob.glob(pattern)
                matches = [m for m in matches if not _is_uninstaller(m)]
                if matches:
                    best = min(matches, key=len)
                    logger.info("app_launcher: found in vendor dir: %s", best)
                    return best

    return None


def _check_path(exe_name: str) -> Optional[str]:
    """Check if the executable is available in the system PATH."""
    try:
        result = subprocess.run(
            ["where", exe_name],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode == 0:
            path = result.stdout.strip().split("\n")[0].strip()
            if path and os.path.isfile(path):
                logger.info("app_launcher: found in PATH: %s", path)
                return path
    except Exception:
        pass
    return None


def _search_start_menu(name_lower: str, exe_names: list[str]) -> Optional[str]:
    """Search Start Menu shortcuts for the application."""
    try:
        start_menu_dirs = [
            os.path.join(os.environ.get("APPDATA", ""),
                         r"Microsoft\Windows\Start Menu\Programs"),
            os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                         r"Microsoft\Windows\Start Menu\Programs"),
        ]

        for sm_dir in start_menu_dirs:
            if not os.path.isdir(sm_dir):
                continue

            for root_dir, dirs, files in os.walk(sm_dir):
                for fname in files:
                    if not fname.lower().endswith(".lnk"):
                        continue
                    # Check if shortcut name matches.
                    shortcut_name = fname.lower().rstrip(".lnk").rstrip(" ")
                    if name_lower in shortcut_name or shortcut_name in name_lower:
                        # Try to resolve the .lnk target.
                        lnk_path = os.path.join(root_dir, fname)
                        target = _resolve_lnk(lnk_path)
                        if target and os.path.isfile(target) and not _is_uninstaller(target):
                            logger.info("app_launcher: found via Start Menu: %s", target)
                            return target
    except Exception as exc:
        logger.debug("app_launcher: Start Menu search error: %s", exc)
    return None


def _resolve_lnk(lnk_path: str) -> Optional[str]:
    """Resolve a Windows .lnk shortcut to its target path."""
    try:
        import ctypes
        from ctypes import wintypes

        # Use COM to resolve the shortcut.
        import ctypes
        ctypes.windll.ole32.CoInitialize(0)

        # Try PowerShell as fallback (more reliable).
        result = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk_path}').TargetPath"
            ],
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode == 0:
            target = result.stdout.strip()
            if target:
                return target
    except Exception:
        pass
    return None
