#!/usr/bin/env python3
# /// script
# dependencies = ["requests>=2.28.0"]
# ///
"""
GPT Image 2 CLI wrapper for image-forge skill.
Two-tier fallback chain (GPT Image 2 only, no Gemini):
  1. CRS proxy             (CRS_BASE_URL + CRS_API_KEY)
  2. openclaw infer image  (Codex OAuth via OpenClaw — mode=oauth transport=codex-responses)

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

# ─── Backend config ──────────────────────────────────────────────────────────

CRS_BASE = os.environ.get("CRS_BASE_URL", "http://127.0.0.1:8765")
CRS_KEY  = os.environ.get("CRS_API_KEY", "")

VALID_SIZES = [
    "1024x1024", "1536x1024", "1024x1536",
    "2048x2048", "3840x2160", "2160x3840",
]
DEFAULT_SIZE_GENERATE = "1536x1024"
DEFAULT_SIZE_EDIT     = "1024x1536"

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


# ─── Tier-1: CRS ─────────────────────────────────────────────────────────────

def _api_call(base_url: str, endpoint: str, headers: dict,
              payload: dict, timeout: int) -> dict | None:
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
        log(f"Auth error (401) at {base_url}")
        return None

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
        if resp.status_code in (400, 415):
            print(f"Fatal API error: {msg}", file=sys.stderr)
            sys.exit(1)
        log(f"API error from {base_url}: {msg}")
        return None

    if "data" not in d or not d["data"]:
        log(f"Unexpected response from {base_url}: {d}")
        return None

    return d


def try_crs(endpoint: str, payload: dict, timeout: int) -> dict | None:
    if not CRS_KEY:
        log("CRS_API_KEY not set — skipping tier-1")
        return None
    log(f"Tier-1: CRS → {CRS_BASE}{endpoint}")
    return _api_call(CRS_BASE, endpoint, {"Authorization": f"Bearer {CRS_KEY}"},
                     payload, timeout)


# ─── Tier-2: openclaw infer image (Codex OAuth) ───────────────────────────────

def try_openclaw_infer(mode: str, payload: dict, timeout: int,
                       image_paths: list[str] | None = None) -> str | None:
    """
    Uses `openclaw infer image generate/edit` which internally routes through
    Codex OAuth (mode=oauth, transport=codex-responses).
    Returns output file path on success, None on any failure.
    """
    out_path = payload.get("_output") or f"/tmp/gpt-image2-oc-{int(time.time())}.png"

    cmd = ["openclaw", "infer", "image", mode,
           "--prompt", payload["prompt"],
           "--model", "openai/gpt-image-2",
           "--output", out_path,
           "--json"]

    if payload.get("size"):
        cmd += ["--size", payload["size"]]

    if mode == "edit":
        for img in (image_paths or []):
            cmd += ["--file", img]

    log(f"Tier-2: openclaw infer image {mode} (Codex OAuth)")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            log(f"openclaw infer failed (rc={result.returncode}): {result.stderr[:300]}")
            return None
        data = json.loads(result.stdout)
        if data.get("ok") and data.get("outputs"):
            return data["outputs"][0]["path"]
        log(f"openclaw infer unexpected output: {result.stdout[:200]}")
        return None
    except subprocess.TimeoutExpired:
        log(f"openclaw infer timeout ({timeout}s)")
        return None
    except FileNotFoundError:
        log("openclaw CLI not found — skipping tier-2")
        return None
    except Exception as e:
        log(f"openclaw infer error: {e}")
        return None


# ─── Dispatch ─────────────────────────────────────────────────────────────────

def dispatch(endpoint: str, payload: dict, args: argparse.Namespace,
             image_paths: list[str] | None = None) -> None:
    """
    Tier-1: CRS → Tier-2: openclaw infer (Codex OAuth).
    GPT Image 2 only. No Gemini fallback.
    """
    # Tier-1: CRS
    result = try_crs(endpoint, payload, args.timeout)
    if result is not None:
        item = result["data"][0]
        b64 = item.get("b64_json", "")
        if not b64:
            print("Error: no b64_json in CRS response", file=sys.stderr)
            sys.exit(1)
        out_path = save_b64(b64, args.output, args.format)
        print(f"MEDIA: {os.path.abspath(out_path)}")
        if item.get("revised_prompt"):
            log(f"revised_prompt: {item['revised_prompt'][:200]}")
        return

    # Tier-2: openclaw infer (Codex OAuth)
    mode = "edit" if image_paths else "generate"
    payload["_output"] = args.output or f"/tmp/gpt-image2-oc-{int(time.time())}.{args.format}"
    out_path = try_openclaw_infer(mode, payload, args.timeout, image_paths)
    if out_path:
        print(f"MEDIA: {os.path.abspath(out_path)}")
        return

    print("Error: all GPT Image 2 backends exhausted (CRS + openclaw infer both failed)",
          file=sys.stderr)
    sys.exit(1)


# ─── Commands ─────────────────────────────────────────────────────────────────

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
    images = [{"image_url": f"data:{detect_mime(p)};base64,{read_image_b64(p)}"}
              for p in args.image]
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


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="GPT Image 2 CLI — CRS (Tier-1) + openclaw infer Codex OAuth (Tier-2)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("-p", "--prompt", required=True)
    shared.add_argument("-o", "--output", default="")
    shared.add_argument("--size", choices=VALID_SIZES, default="")
    shared.add_argument("--quality", choices=["standard", "high"], default="high")
    shared.add_argument("--format", choices=["png", "webp", "jpeg"], default="png",
                        dest="format")
    shared.add_argument("--timeout", type=int, default=320)  # 300s CRS + 20s buffer

    gen = sub.add_parser("generate", parents=[shared])
    gen.add_argument("--background", choices=["transparent", "white", "auto"], default="")

    edit = sub.add_parser("edit", parents=[shared])
    edit.add_argument("-i", "--image", action="append", metavar="PATH",
                      help="Reference image (repeat for multiple, max 4)")

    args = parser.parse_args()
    if args.command == "generate":
        cmd_generate(args)
    elif args.command == "edit":
        cmd_edit(args)


if __name__ == "__main__":
    main()
