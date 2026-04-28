#!/usr/bin/env python3
# /// script
# dependencies = ["requests>=2.28.0"]
# ///
"""
GPT Image 2 CLI wrapper for image-forge skill.
Three-tier fallback chain:
  1. CRS proxy         (CRS_BASE_URL + CRS_API_KEY)
  2. Codex OAuth       (~/.codex/auth.json access_token → api.openai.com)
  3. Gemini fallback   (generate_image.py — Nano Banana 2)

Usage:
  python gpt_image2.py generate --prompt "..." --output /path/out.png [--size 1536x1024] [--quality high]
  python gpt_image2.py edit     --prompt "..." -i ref.png --output /path/out.png
  python gpt_image2.py edit     --prompt "..." -i ref1.png -i ref2.png --output /path/out.png

Environment:
  CRS_BASE_URL  CRS service base URL (default: http://127.0.0.1:8765)
  CRS_API_KEY   CRS API key (required for tier-1)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests", file=sys.stderr)
    sys.exit(1)

# ─── Backend config ─────────────────────────────────────────────────────────

CRS_BASE    = os.environ.get("CRS_BASE_URL", "http://127.0.0.1:8765")
CRS_KEY     = os.environ.get("CRS_API_KEY", "")
OPENAI_BASE = "https://api.openai.com"
CODEX_AUTH  = Path.home() / ".codex" / "auth.json"

VALID_SIZES = [
    "1024x1024", "1536x1024", "1024x1536",
    "2048x2048", "3840x2160", "2160x3840",
]
DEFAULT_SIZE_GENERATE = "1536x1024"
DEFAULT_SIZE_EDIT     = "1024x1536"

# size → Gemini aspect ratio mapping (best-effort)
SIZE_TO_ASPECT = {
    "1024x1024": "1:1",
    "1536x1024": "16:9",
    "1024x1536": "9:16",
    "2048x2048": "1:1",
    "3840x2160": "16:9",
    "2160x3840": "9:16",
}

# ─── Retry-worthy error codes ────────────────────────────────────────────────

RETRYABLE_HTTP = {429, 500, 502, 503, 504}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def read_image_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def detect_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp"}.get(ext, "image/png")


def save_b64(b64: str, output: str, fmt: str = "png") -> str:
    out_path = output or f"/tmp/gpt-image2-{int(time.time())}.{fmt}"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(base64.b64decode(b64))
    return out_path


def log(msg: str) -> None:
    print(f"[image-forge] {msg}", file=sys.stderr)


# ─── Codex OAuth token ───────────────────────────────────────────────────────

def load_codex_token() -> str | None:
    """Read access_token from ~/.codex/auth.json. Returns None if unavailable."""
    if not CODEX_AUTH.exists():
        return None
    try:
        data = json.loads(CODEX_AUTH.read_text())
        token = data.get("tokens", {}).get("access_token", "")
        return token if token else None
    except Exception as e:
        log(f"Codex auth read error: {e}")
        return None


# ─── Core API call ───────────────────────────────────────────────────────────

def _api_call(base_url: str, endpoint: str, headers: dict,
              payload: dict, timeout: int) -> dict | None:
    """
    POST to base_url + endpoint.
    Returns parsed JSON dict on success, None on retryable error,
    raises on fatal / auth errors.
    """
    url = f"{base_url}{endpoint}"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.ConnectionError as e:
        log(f"Connection error to {base_url}: {e}")
        return None
    except requests.exceptions.Timeout:
        log(f"Timeout ({timeout}s) calling {base_url}")
        return None

    if resp.status_code == 401:
        log(f"Auth error (401) at {base_url} — token invalid/expired")
        return None  # treat as non-fatal → try next tier

    if resp.status_code in RETRYABLE_HTTP:
        log(f"HTTP {resp.status_code} from {base_url} — retryable")
        return None

    try:
        d = resp.json()
    except Exception:
        log(f"Non-JSON response (HTTP {resp.status_code}) from {base_url}")
        return None

    if "error" in d:
        err = d["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        # content policy / invalid param → fatal, don't retry
        if resp.status_code in (400, 415):
            print(f"Fatal API error: {msg}", file=sys.stderr)
            sys.exit(1)
        log(f"API error from {base_url}: {msg}")
        return None

    if "data" not in d or not d["data"]:
        log(f"Unexpected response from {base_url}: {d}")
        return None

    return d


# ─── Tier-1: CRS ─────────────────────────────────────────────────────────────

def try_crs(endpoint: str, payload: dict, timeout: int) -> dict | None:
    if not CRS_KEY:
        log("CRS_API_KEY not set — skipping tier-1")
        return None
    log(f"Tier-1: CRS → {CRS_BASE}{endpoint}")
    headers = {"Authorization": f"Bearer {CRS_KEY}"}
    return _api_call(CRS_BASE, endpoint, headers, payload, timeout)


# ─── Tier-2: Codex OAuth ─────────────────────────────────────────────────────

def try_codex_oauth(endpoint: str, payload: dict, timeout: int) -> dict | None:
    """
    NOTE: Codex OAuth tokens (auth_mode=chatgpt) are ChatGPT account tokens,
    NOT OpenAI Platform API keys. They cannot call api.openai.com directly.
    This tier is intentionally disabled until a viable exchange mechanism is found.
    Keeping the function for future use when Codex exposes a usable API token.
    """
    log("Tier-2: Codex OAuth — disabled (ChatGPT token ≠ Platform API key, skipping)")
    return None


# ─── Tier-3: Gemini fallback ─────────────────────────────────────────────────

def try_gemini_fallback(prompt: str, size: str, output: str,
                        image_paths: list[str]) -> None:
    """
    Delegate to generate_image.py (Nano Banana 2 / Gemini backend).
    For edit mode, passes reference images with -i flags.
    """
    log("Tier-3: Gemini fallback → generate_image.py")

    script = Path(__file__).parent / "generate_image.py"
    if not script.exists():
        print("Error: generate_image.py not found — all backends exhausted", file=sys.stderr)
        sys.exit(1)

    aspect = SIZE_TO_ASPECT.get(size or DEFAULT_SIZE_GENERATE, "16:9")
    out = output or f"/tmp/gpt-image2-gemini-{int(time.time())}.png"

    cmd = [
        sys.executable, str(script),
        "--prompt", prompt,
        "--filename", out,
        "--aspect-ratio", aspect,
    ]
    for img in image_paths:
        cmd += ["-i", img]

    log(f"Gemini cmd: {' '.join(cmd[:6])} ...")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        print("Error: Gemini fallback also failed — all backends exhausted", file=sys.stderr)
        sys.exit(1)
    # generate_image.py prints MEDIA: path itself


# ─── Unified dispatch ─────────────────────────────────────────────────────────

def dispatch(endpoint: str, payload: dict, args: argparse.Namespace,
             image_paths: list[str] | None = None) -> None:
    """
    Try tier-1 → tier-2 → tier-3 in order.
    On success, print MEDIA: path and exit 0.
    """
    result = try_crs(endpoint, payload, args.timeout)

    if result is None:
        result = try_codex_oauth(endpoint, payload, args.timeout)

    if result is None:
        # Tier-3: Gemini (no structured JSON result, script prints MEDIA itself)
        size = payload.get("size", DEFAULT_SIZE_GENERATE)
        try_gemini_fallback(
            prompt=payload["prompt"],
            size=size,
            output=args.output,
            image_paths=image_paths or [],
        )
        return  # generate_image.py handled output

    # Success from tier-1 or tier-2
    item = result["data"][0]
    b64 = item.get("b64_json", "")
    if not b64:
        print("Error: no b64_json in response", file=sys.stderr)
        sys.exit(1)

    out_path = save_b64(b64, args.output, args.format)
    print(f"MEDIA: {os.path.abspath(out_path)}")

    if item.get("revised_prompt"):
        log(f"revised_prompt: {item['revised_prompt'][:200]}")


# ─── Commands ────────────────────────────────────────────────────────────────

def cmd_generate(args: argparse.Namespace) -> None:
    payload: dict = {
        "model": "gpt-image-2",
        "prompt": args.prompt,
        "size": args.size or DEFAULT_SIZE_GENERATE,
        "quality": args.quality,
        "output_format": args.format,
        "response_format": "b64_json",
    }
    if args.background:
        payload["background"] = args.background

    dispatch("/openai/v1/images/generations", payload, args)


def cmd_edit(args: argparse.Namespace) -> None:
    if not args.image:
        print("Error: --image required for edit", file=sys.stderr)
        sys.exit(1)

    images = []
    for img_path in args.image:
        mime = detect_mime(img_path)
        b64 = read_image_b64(img_path)
        images.append({"image_url": f"data:{mime};base64,{b64}"})

    payload: dict = {
        "model": "gpt-image-2",
        "prompt": args.prompt,
        "images": images,
        "size": args.size or DEFAULT_SIZE_EDIT,
        "quality": args.quality,
        "output_format": args.format,
        "response_format": "b64_json",
    }

    dispatch("/openai/v1/images/edits", payload, args, image_paths=args.image)


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GPT Image 2 CLI for image-forge (3-tier fallback)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-p", "--prompt", required=True)
    shared.add_argument("-o", "--output", default="")
    shared.add_argument("--size", choices=VALID_SIZES, default="")
    shared.add_argument("--quality", choices=["standard", "high"], default="high")
    shared.add_argument(
        "--format", choices=["png", "webp", "jpeg"], default="png", dest="format"
    )
    shared.add_argument("--timeout", type=int, default=320)  # CRS timeout is 300s; give extra buffer

    gen = sub.add_parser("generate", parents=[shared])
    gen.add_argument(
        "--background", choices=["transparent", "white", "auto"], default=""
    )

    edit = sub.add_parser("edit", parents=[shared])
    edit.add_argument(
        "-i", "--image", action="append", metavar="PATH",
        help="Reference image path (repeat for multiple, max 4)"
    )

    args = parser.parse_args()

    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "edit":
        cmd_edit(args)


if __name__ == "__main__":
    main()
