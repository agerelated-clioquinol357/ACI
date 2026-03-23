/*
 * OpenClaw Kill Switch - Emergency Process Terminator
 *
 * Design: Low-level keyboard hook (SetWindowsHookEx WH_KEYBOARD_LL)
 * that monitors for Ctrl+Alt+Shift+Q globally.
 *
 * On trigger: enumerate and SIGKILL all OpenClaw worker processes,
 * release all I/O locks, and notify the daemon.
 *
 * For production use, compile with: cl kill_switch.c /link user32.lib
 *
 * Python alternative: see kill_switch_py.py which uses ctypes to achieve
 * the same via a high-priority thread.
 */
