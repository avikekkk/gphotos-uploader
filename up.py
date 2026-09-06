#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "gpmc",
#     "requests",  # only for --links (Worker call)
# ]
# ///
"""
Standalone Google Photos uploader.

    uv run up.py --file video.mp4
    uv run up.py video.mp4                 # positional works too
    uv run up.py a.mp4 b.jpg c.mkv         # several files
    uv run up.py --dir ./clips             # every media file in a folder (recursive)
    uv run up.py --file video.mp4 --links  # also create a public download link (Worker)

(Plain `python up.py ...` works too if gpmc is already installed.)

Auth is read from, in order:
    1. --auth "<string>"
    2. GP_AUTH_DATA environment variable
    3. ANDROID_CREDS in config.py (same folder)

Generate an auth_data string once with:
    uv run get_auth_data.py "<oauth_token>"
"""

import argparse
import os
import sys
from urllib.parse import parse_qsl, quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Formats Google Photos accepts, mirroring gotohp's allowlist
# (github.com/xob0t/gotohp, backend/upload.go). Anything else Google rejects.
MEDIA_EXTS = {
    # photos
    ".avif", ".bmp", ".gif", ".heic", ".heif", ".ico", ".jpg", ".jpeg",
    ".png", ".tif", ".tiff", ".webp",
    # raw photos
    ".cr2", ".cr3", ".nef", ".arw", ".orf", ".raf", ".rw2", ".pef",
    ".sr2", ".dng",
    # videos
    ".3gp", ".3g2", ".asf", ".avi", ".divx", ".m2t", ".m2ts", ".m4v",
    ".mkv", ".mmv", ".mod", ".mov", ".mp4", ".mpg", ".mpeg", ".mts",
    ".tod", ".wmv", ".ts", ".webm",
}


# ── output helpers ───────────────────────────────────────────────────────────

def info(msg: str) -> None:
    print(f"[*] {msg}")


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def warn(msg: str) -> None:
    print(f"[!] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)


def die(msg: str) -> "None":
    fail(msg)
    sys.exit(1)


def format_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


# ── core ─────────────────────────────────────────────────────────────────────

def load_auth(cli_auth: str | None) -> str:
    if cli_auth:
        return cli_auth.strip()
    env = os.getenv("GP_AUTH_DATA")
    if env:
        return env.strip()
    try:
        import config
        if getattr(config, "ANDROID_CREDS", ""):
            return config.ANDROID_CREDS.strip()
    except Exception:
        pass
    die(
        "No auth_data found. Provide --auth, set GP_AUTH_DATA, or fill "
        "ANDROID_CREDS in config.py. Generate it with: "
        'uv run get_auth_data.py "<oauth_token>"'
    )


def account_email(auth_data: str) -> str:
    try:
        return dict(parse_qsl(auth_data)).get("Email", "unknown")
    except Exception:
        return "unknown"


def collect_targets(files: list[str], directory: str | None) -> list[str]:
    targets: list[str] = []
    for f in files:
        if not os.path.isfile(f):
            warn(f"Skipping (not a file): {f}")
            continue
        targets.append(os.path.abspath(f))
    if directory:
        if not os.path.isdir(directory):
            die(f"Not a directory: {directory}")
        # Walk the whole tree so nested subfolders are included.
        for root, dirs, names in os.walk(directory):
            dirs.sort()
            for name in sorted(names):
                p = os.path.join(root, name)
                if os.path.isfile(p) and os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                    targets.append(os.path.abspath(p))
    # de-duplicate, preserve order
    seen: set[str] = set()
    unique = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return unique


def media_key_of(result, path: str) -> str | None:
    """gpmc upload() returns {abs_path: media_key}; be tolerant of shapes."""
    if isinstance(result, dict):
        if path in result:
            return str(result[path])
        for v in result.values():
            if isinstance(v, str):
                return v
    if isinstance(result, str):
        return result
    return None


def _cfg(name: str, env: str) -> str:
    v = os.getenv(env)
    if v:
        return v.strip()
    try:
        import config
        return str(getattr(config, name, "") or "").strip()
    except Exception:
        return ""


def worker_link(client, media_key: str, email: str, filename: str) -> str | None:
    """Resolve the upload's Google remote URL and register it with the Worker.

    Returns a clean public download link, or None if the Worker isn't
    configured or the remote URL can't be found.
    """
    import sqlite3
    from pathlib import Path
    import requests

    base = _cfg("WORKER_BASE", "WORKER_BASE").rstrip("/")
    secret = _cfg("SHORTEN_SECRET", "SHORTEN_SECRET")
    if not base or not secret:
        warn("Links skipped: set WORKER_BASE and SHORTEN_SECRET in config.py.")
        return None

    # gpmc caches the library (media_key -> remote_url, type) in a local SQLite DB.
    db = Path.home() / ".gpmc" / email / "storage.db"
    remote_url, mtype = None, None
    for attempt in (1, 2):
        if db.exists():
            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "SELECT remote_url, type FROM remote_media WHERE media_key=?",
                    (media_key,),
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                remote_url, mtype = row[0], row[1]
                break
        if attempt == 1:
            try:
                client.update_cache(show_progress=False)  # pick up the new item
            except Exception:
                pass
    if not remote_url:
        warn("Links skipped: could not resolve the item's remote URL yet.")
        return None

    # Google serves videos with =dv and photos with =d. type 2 == video.
    suffix = "=dv" if mtype == 2 else "=d"

    try:
        r = requests.post(
            f"{base}/shorten",
            json={"url": remote_url, "suffix": suffix,
                  "secret": secret, "filename": filename},
            timeout=20,
        )
        if r.status_code != 200:
            warn(f"Worker /shorten returned HTTP {r.status_code}.")
            return None
        sid = r.json().get("id")
    except Exception as e:
        warn(f"Worker request failed: {e}")
        return None
    if not sid:
        warn("Worker did not return a link id.")
        return None
    return f"{base}/{sid}/{quote(filename)}"


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="up.py",
        description="Upload files to Google Photos.",
    )
    ap.add_argument("files", nargs="*", help="file(s) to upload")
    ap.add_argument("--file", action="append", default=[], metavar="PATH",
                    help="a file to upload (repeatable)")
    ap.add_argument("--dir", metavar="DIR",
                    help="upload every media file in this folder, recursively")
    ap.add_argument("--auth", metavar="STR", help="gpmc auth_data string (overrides env/config)")
    ap.add_argument("--threads", type=int, default=4, help="parallel upload threads (default: 4)")
    ap.add_argument("--no-progress", action="store_true", help="hide the gpmc progress bar")
    ap.add_argument("--links", action="store_true",
                    help="also create a public download link via the Cloudflare Worker")
    args = ap.parse_args()

    targets = collect_targets(args.files + args.file, args.dir)
    if not targets:
        ap.error("no files to upload - pass a --file, positional path(s), or --dir")

    auth = load_auth(args.auth)

    try:
        from gpmc import Client
    except ImportError:
        die("gpmc is not installed. Use `uv run up.py ...` or `pip install gpmc`.")

    client = Client(auth)

    total = len(targets)
    total_bytes = sum(os.path.getsize(p) for p in targets)
    info(f"Account : {account_email(auth)}")
    info(f"Files   : {total} ({format_bytes(total_bytes)})")
    info(f"Threads : {args.threads}")
    print("-" * 60)

    ok_count = 0
    fail_count = 0
    for idx, path in enumerate(targets, 1):
        name = os.path.basename(path)
        size = format_bytes(os.path.getsize(path))
        print(f"[{idx}/{total}] {name} ({size})")
        try:
            result = client.upload(
                target=path,
                show_progress=not args.no_progress,
                threads=args.threads,
            )
            key = media_key_of(result, path)
            if not key:
                raise RuntimeError(f"no media key returned: {result!r}")

            gp_url = f"https://photos.google.com/photo/{key}"
            print(f"        photos : {gp_url}")

            if args.links:
                dl = worker_link(client, key, account_email(auth), name)
                if dl:
                    print(f"        link   : {dl}")

            ok_count += 1
        except Exception as e:
            fail(f"{name}: {e}")
            fail_count += 1
        print()

    print("-" * 60)
    info(f"Done. {ok_count} uploaded, {fail_count} failed.")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    main()
