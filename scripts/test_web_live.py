"""
OpenClaw ACI - Live Web Bridge Test Suite.

Drives the running daemon+worker via HTTP API to validate:
1. Action response times (should be <5s, not 120s)
2. Sequence actions (type+Enter as atomic operation)
3. Quick perceive mode
4. End-to-end Bilibili search flow

Usage:
    python scripts/test_web_live.py [--test basic|bilibili|all] [--base-url URL]

Prerequisites:
    - Daemon running: python -m core.server
    - Web bridge worker running: python -m bridges.web_bridge.worker
"""
from __future__ import annotations

import argparse
import json
import sys
import time

import requests

BASE_URL = "http://127.0.0.1:11434/v1"
SESSION_ID = "live-test"
PASS_THRESHOLD_S = 10.0  # Actions should complete well under this


def _post(endpoint: str, payload: dict, timeout: float = 30.0) -> dict:
    """POST to daemon and return JSON response."""
    try:
        res = requests.post(f"{BASE_URL}/{endpoint}", json=payload, timeout=timeout)
        return res.json()
    except requests.exceptions.ConnectionError:
        return {"error": f"Connection refused. Is daemon running at {BASE_URL}?"}
    except requests.exceptions.Timeout:
        return {"error": f"Request timed out after {timeout}s"}
    except Exception as exc:
        return {"error": str(exc)}


def _timed_post(endpoint: str, payload: dict, timeout: float = 30.0) -> tuple[dict, float]:
    """POST and return (response, elapsed_seconds)."""
    start = time.monotonic()
    result = _post(endpoint, payload, timeout=timeout)
    elapsed = time.monotonic() - start
    return result, elapsed


def _print_result(name: str, passed: bool, elapsed: float, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {name} ({elapsed:.2f}s){f' - {detail}' if detail else ''}")


# ---------------------------------------------------------------------------
# Test: Basic connectivity
# ---------------------------------------------------------------------------

def test_health() -> bool:
    """Check daemon is reachable."""
    try:
        res = requests.get(f"{BASE_URL.rsplit('/v1', 1)[0]}/health", timeout=5)
        passed = res.status_code == 200
        _print_result("health_check", passed, 0, f"status={res.status_code}")
        return passed
    except Exception as exc:
        _print_result("health_check", False, 0, str(exc))
        return False


def test_create_session(url: str = "https://www.bilibili.com") -> bool:
    """Create a web session and navigate to URL."""
    result, elapsed = _timed_post("session", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "target_url": url,
    }, timeout=30)
    # Session may already exist — that's OK
    passed = "error" not in result or "already" in str(result.get("error", "")).lower()
    _print_result("create_session", passed, elapsed, json.dumps(result, ensure_ascii=True)[:100])
    return passed


# ---------------------------------------------------------------------------
# Test: Action response time (the critical fix)
# ---------------------------------------------------------------------------

def test_perceive_response_time() -> bool:
    """Perceive should return in reasonable time."""
    result, elapsed = _timed_post("perceive", {
        "session_id": SESSION_ID,
        "context_env": "web",
    }, timeout=30)
    elements = result.get("elements", [])
    passed = elapsed < PASS_THRESHOLD_S and "error" not in result
    _print_result("perceive_response_time", passed, elapsed,
                  f"{len(elements)} elements")
    return passed


def test_click_response_time() -> bool:
    """Click action should return in <5s (not 120s)."""
    # First perceive to get element refs
    perc = _post("perceive", {"session_id": SESSION_ID, "context_env": "web"}, timeout=30)
    elements = perc.get("elements", [])
    if not elements:
        _print_result("click_response_time", False, 0, "No elements from perceive")
        return False

    # Find a clickable element
    target = None
    for el in elements:
        if el.get("interactable") and el.get("bbox"):
            target = el["uid"]
            break
    if not target:
        _print_result("click_response_time", False, 0, "No clickable element found")
        return False

    result, elapsed = _timed_post("action", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "action_type": "click",
        "target_uid": target,
    }, timeout=15)
    passed = elapsed < PASS_THRESHOLD_S and result.get("success", False)
    _print_result("click_response_time", passed, elapsed,
                  f"target={target}, success={result.get('success')}")
    return passed


def test_type_response_time() -> bool:
    """Type action should return in <5s."""
    # First perceive to find search box
    perc = _post("perceive", {"session_id": SESSION_ID, "context_env": "web"}, timeout=30)
    elements = perc.get("elements", [])

    target = None
    for el in elements:
        role = el.get("role", "")
        tag = el.get("tag", "")
        if role in ("textbox", "searchbox") or tag in ("input", "textbox"):
            target = el["uid"]
            break
    if not target:
        _print_result("type_response_time", False, 0, "No text input found")
        return False

    result, elapsed = _timed_post("action", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "action_type": "type",
        "target_uid": target,
        "value": "test",
    }, timeout=15)
    passed = elapsed < PASS_THRESHOLD_S and result.get("success", False)
    _print_result("type_response_time", passed, elapsed,
                  f"target={target}, success={result.get('success')}")
    return passed


def test_press_key_response_time() -> bool:
    """Press key should return in <5s."""
    result, elapsed = _timed_post("action", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "action_type": "press_key",
        "value": "Escape",
    }, timeout=15)
    passed = elapsed < PASS_THRESHOLD_S and result.get("success", False)
    _print_result("press_key_response_time", passed, elapsed,
                  f"success={result.get('success')}")
    return passed


# ---------------------------------------------------------------------------
# Test: Sequence action (atomic type+Enter)
# ---------------------------------------------------------------------------

def test_sequence_action() -> bool:
    """Sequence action: type+Enter in one round-trip."""
    # Find search box
    perc = _post("perceive", {"session_id": SESSION_ID, "context_env": "web"}, timeout=30)
    elements = perc.get("elements", [])

    target = None
    for el in elements:
        role = el.get("role", "")
        tag = el.get("tag", "")
        if role in ("textbox", "searchbox") or tag in ("input", "textbox"):
            target = el["uid"]
            break
    if not target:
        _print_result("sequence_action", False, 0, "No text input found")
        return False

    seq_value = json.dumps([
        {"action_type": "type", "target_uid": target, "value": "jay"},
        {"action_type": "press_key", "value": "Enter"},
    ])

    result, elapsed = _timed_post("action", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "action_type": "sequence",
        "value": seq_value,
    }, timeout=15)
    passed = elapsed < PASS_THRESHOLD_S and result.get("success", False)
    _print_result("sequence_action", passed, elapsed,
                  f"success={result.get('success')}, msg={result.get('message', '')[:80]}")
    return passed


# ---------------------------------------------------------------------------
# Test: Quick perceive
# ---------------------------------------------------------------------------

def test_quick_perceive() -> bool:
    """Quick perceive should be very fast (<2s)."""
    result, elapsed = _timed_post("perceive", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "perceive_mode": "quick",
    }, timeout=10)
    url = result.get("current_url", "")
    passed = elapsed < 2.0 and "error" not in result
    _print_result("quick_perceive", passed, elapsed, f"url={url[:60]}")
    return passed


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------

def run_basic_tests() -> int:
    """Run basic connectivity and response time tests."""
    print("\n=== Basic Tests ===")
    results = [
        test_health(),
        test_create_session("https://www.bilibili.com"),
    ]
    if not all(results):
        print("\n  Infrastructure not ready. Fix above issues first.")
        return sum(1 for r in results if not r)

    time.sleep(3)  # Wait for page load

    results.extend([
        test_perceive_response_time(),
        test_click_response_time(),
        test_type_response_time(),
        test_press_key_response_time(),
        test_quick_perceive(),
    ])

    passed = sum(1 for r in results if r)
    total = len(results)
    print(f"\n  Results: {passed}/{total} passed")
    return total - passed


def run_bilibili_test() -> int:
    """Run end-to-end Bilibili search test."""
    print("\n=== Bilibili End-to-End Test ===")

    # Step 1: Create session and navigate
    if not test_create_session("https://www.bilibili.com"):
        return 1

    time.sleep(3)

    # Step 2: Perceive to get search box
    print("  [INFO] Perceiving page...")
    perc, elapsed = _timed_post("perceive", {
        "session_id": SESSION_ID, "context_env": "web",
    }, timeout=30)
    elements = perc.get("elements", [])
    print(f"  [INFO] Got {len(elements)} elements in {elapsed:.2f}s")

    # Step 3: Find search input
    search_uid = None
    for el in elements:
        role = el.get("role", "")
        text = el.get("text", "").lower()
        tag = el.get("tag", "")
        if role in ("textbox", "searchbox") or "search" in text or (tag == "input" and "search" in str(el.get("attributes", {}))):
            search_uid = el["uid"]
            print(f"  [INFO] Found search box: {search_uid} (role={role}, text={el.get('text', '')[:30]})")
            break

    if not search_uid:
        _print_result("find_search_box", False, 0, "No search input found in elements")
        return 1
    _print_result("find_search_box", True, elapsed)

    # Step 4: Sequence type + Enter
    print("  [INFO] Executing sequence: type('jay') + Enter...")
    seq_value = json.dumps([
        {"action_type": "type", "target_uid": search_uid, "value": "jay"},
        {"action_type": "press_key", "value": "Enter"},
    ])
    result, elapsed = _timed_post("action", {
        "session_id": SESSION_ID,
        "context_env": "web",
        "action_type": "sequence",
        "value": seq_value,
    }, timeout=15)
    passed = result.get("success", False) and elapsed < PASS_THRESHOLD_S
    _print_result("sequence_type_enter", passed, elapsed,
                  f"success={result.get('success')}, msg={result.get('message', '')[:60]}")
    if not passed:
        return 1

    # Step 5: Quick perceive to check URL changed
    time.sleep(2)  # Wait for navigation
    print("  [INFO] Quick perceive to verify navigation...")
    qp, elapsed = _timed_post("perceive", {
        "session_id": SESSION_ID, "context_env": "web", "perceive_mode": "quick",
    }, timeout=10)
    url = qp.get("current_url", "")
    search_happened = "search" in url.lower() or "jay" in url.lower()
    _print_result("search_navigation", search_happened, elapsed, f"url={url[:80]}")

    return 0 if search_happened else 1


def main():
    parser = argparse.ArgumentParser(description="OpenClaw ACI Live Web Bridge Tests")
    parser.add_argument("--test", choices=["basic", "bilibili", "all"], default="all")
    parser.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    args = parser.parse_args()

    global BASE_URL
    BASE_URL = args.base_url

    failures = 0
    if args.test in ("basic", "all"):
        failures += run_basic_tests()
    if args.test in ("bilibili", "all"):
        failures += run_bilibili_test()

    print(f"\n{'=' * 40}")
    if failures == 0:
        print("ALL TESTS PASSED")
    else:
        print(f"FAILURES: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
