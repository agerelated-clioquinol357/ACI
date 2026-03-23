import argparse
import requests
import json
import sys

BASE_URL = "http://127.0.0.1:11434/v1"

_B64_FIELDS = {"verification_screenshot", "visual_reference_image", "screenshot_b64", "image_b64"}

def _strip_base64(obj):
    """Replace raw base64 blobs with a truncated placeholder to keep output readable."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _B64_FIELDS and isinstance(v, str) and len(v) > 200 and not v.startswith("[screenshot saved:"):
                out[k] = f"[base64 truncated, {len(v)} chars]"
            else:
                out[k] = _strip_base64(v)
        return out
    if isinstance(obj, list):
        return [_strip_base64(item) for item in obj]
    return obj

def print_result(data):
    """Print JSON result to stdout. Use ensure_ascii=True to bypass Windows encoding issues."""
    try:
        # Forcing ensure_ascii=True makes the pipe 100% safe for cross-process transmission.
        output = json.dumps(_strip_base64(data), indent=2, ensure_ascii=True)
        print(output)
    except Exception as exc:
        print(json.dumps({"error": f"Output serialization failed: {exc}"}))

def main():
    # Force UTF-8 on stdout/stderr to avoid GBK encoding errors on Chinese Windows.
    import io as _io
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        # Fallback for older Python versions.
        if hasattr(sys.stderr, "buffer"):
            sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="OpenClaw ACI Client CLI")
    parser.add_argument("--session", required=True, help="Session ID")
    parser.add_argument("--env", choices=["web", "desktop", "cli"], default="web", help="Context environment")
    parser.add_argument("--action", choices=["start", "perceive", "act", "screenshot"], required=True, help="Action to perform")
    
    # For 'start'
    parser.add_argument("--url", help="Target URL (for start action)")
    
    # For 'act'
    parser.add_argument("--type", help="Action type (click, type, press_key, scroll, wait)")
    parser.add_argument("--uid", help="Target element UID")
    parser.add_argument("--value", help="Input text or key value")
    parser.add_argument("--force-fallback", action="store_true", help="Force T3 vision fallback")

    # For screenshot
    parser.add_argument("--save-to", help="Save screenshot to file path (optional)")

    # For perceive with ROI
    parser.add_argument("--region", help="Region of interest as x,y,w,h (for perceive)")

    args = parser.parse_args()

    try:
        if args.action == "start":
            payload = {"session_id": args.session, "context_env": args.env}
            if args.url:
                payload["target_url"] = args.url
            res = requests.post(f"{BASE_URL}/session", json=payload, timeout=30)
            print_result(res.json())

        elif args.action == "perceive":
            payload = {"session_id": args.session, "context_env": args.env}
            if args.region:
                try:
                    parts = [int(v.strip()) for v in args.region.split(",")]
                    if len(parts) == 4:
                        payload["region"] = parts
                    else:
                        print(json.dumps({"error": "--region must be x,y,w,h (4 integers)"}, ensure_ascii=False))
                        sys.exit(1)
                except ValueError:
                    print(json.dumps({"error": "--region values must be integers: x,y,w,h"}, ensure_ascii=False))
                    sys.exit(1)
            res = requests.post(f"{BASE_URL}/perceive", json=payload, timeout=120)
            data = res.json()
            # Smart output: show text snapshot (spatial_context) instead of raw JSON blob.
            # The spatial_context is the LLM-friendly text snapshot of all interactive elements.
            # Full JSON is overwhelming for agents; the snapshot is what they need.
            snapshot = data.get("spatial_context") or ""
            if snapshot and not args.region:
                print(f"State: {data.get('state', '?')}")
                print(f"URL: {data.get('current_url', '?')}")
                print(f"Title: {data.get('active_window_title', '?')}")
                print(f"Elements: {len(data.get('elements', []))}")
                print()
                print(snapshot)
                # Also print interrupted_reason if present
                if data.get("interrupted_reason"):
                    print(f"\n⚠ INTERRUPT: {data['interrupted_reason']}")
            else:
                # Fallback: full JSON (for ROI queries or when no snapshot available)
                print_result(data)

        elif args.action == "act":
            if not args.type:
                print(json.dumps({"error": "--type is required for 'act' (e.g. click, type)"}, ensure_ascii=False))
                sys.exit(1)
            payload = {
                "session_id": args.session,
                "context_env": args.env,
                "action_type": args.type
            }
            if args.uid:
                payload["target_uid"] = args.uid
            if args.value:
                payload["value"] = args.value
            if args.force_fallback:
                payload["force_fallback"] = True

            res = requests.post(f"{BASE_URL}/action", json=payload, timeout=120)
            print_result(res.json())

        elif args.action == "screenshot":
            payload = {"session_id": args.session, "context_env": args.env}
            res = requests.post(f"{BASE_URL}/screenshot", json=payload, timeout=30)
            data = res.json()
            if args.save_to and data.get("success") and data.get("image_b64"):
                import base64
                img_bytes = base64.b64decode(data["image_b64"])
                with open(args.save_to, "wb") as f:
                    f.write(img_bytes)
                print(json.dumps({"success": True, "saved_to": args.save_to, "size_bytes": len(img_bytes)}, ensure_ascii=False))
            else:
                print_result(data)

    except requests.exceptions.ConnectionError:
        print(json.dumps({"error": f"Connection refused to {BASE_URL}. Ensure ACI Daemon is running via start_aci.ps1"}, ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()
