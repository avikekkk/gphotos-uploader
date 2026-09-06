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
    uv run up.py --dir ./trip --album Trip # group the uploads into an album
    uv run up.py --dir ./s01 --share-album "Show S01"  # one public album link
    uv run up.py --file video.mp4 --links  # also create a public download link (Worker)
    uv run up.py --file video.mp4 --ddl    # also print Google's direct link (expires)

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


def _key_for_path(result, path: str) -> str | None:
    """Look up one file's media key in gpmc's {path: media_key} batch result.

    Keys may be str or Path and may differ in normalization, so match on the
    resolved absolute path.
    """
    if not isinstance(result, dict):
        return None
    if path in result:
        return str(result[path])
    want = os.path.realpath(path)
    for k, v in result.items():
        if os.path.realpath(str(k)) == want:
            return str(v)
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


def resolve_item(client, media_key: str, email: str) -> dict | None:
    """Look up an uploaded item's Google remote URL, download suffix and size.

    gpmc caches the library (media_key -> remote_url, type, size) in a local
    SQLite DB. Syncs the cache once if the row isn't there yet. Returns
    {"remote_url", "suffix", "type", "size"} or None if it can't be resolved.
    """
    import sqlite3
    from pathlib import Path

    db = Path.home() / ".gpmc" / email / "storage.db"
    remote_url, mtype, size = None, None, None
    for attempt in (1, 2):
        if db.exists():
            con = sqlite3.connect(db)
            try:
                row = con.execute(
                    "SELECT remote_url, type, size_bytes FROM remote_media "
                    "WHERE media_key=?",
                    (media_key,),
                ).fetchone()
            finally:
                con.close()
            if row and row[0]:
                remote_url, mtype, size = row[0], row[1], row[2]
                break
        if attempt == 1:
            try:
                client.update_cache(show_progress=False)  # pick up the new item
            except Exception:
                pass
    if not remote_url:
        return None
    # Google serves videos with =dv and photos with =d. type 2 == video.
    return {
        "remote_url": remote_url,
        "suffix": "=dv" if mtype == 2 else "=d",
        "type": mtype,
        "size": size,
    }


def worker_link(client, media_key: str, email: str, filename: str) -> str | None:
    """Resolve an upload's remote URL and register it with the Worker.

    Returns a clean public download link, or None if the Worker isn't
    configured or the remote URL can't be found.
    """
    import requests

    base = _cfg("WORKER_BASE", "WORKER_BASE").rstrip("/")
    secret = _cfg("SHORTEN_SECRET", "SHORTEN_SECRET")
    if not base or not secret:
        warn("Links skipped: set WORKER_BASE and SHORTEN_SECRET in config.py.")
        return None

    item = resolve_item(client, media_key, email)
    if not item:
        warn("Links skipped: could not resolve the item's remote URL yet.")
        return None

    try:
        r = requests.post(
            f"{base}/shorten",
            json={"url": item["remote_url"], "suffix": item["suffix"],
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


def worker_album(client, name: str, entries: list[tuple[str, str]], email: str) -> str | None:
    """Register a batch of uploads as one Worker album and return its page URL.

    `entries` is a list of (media_key, filename). Returns the album URL, or
    None if the Worker isn't configured or no items could be resolved.
    """
    import requests

    base = _cfg("WORKER_BASE", "WORKER_BASE").rstrip("/")
    secret = _cfg("SHORTEN_SECRET", "SHORTEN_SECRET")
    if not base or not secret:
        warn("Album skipped: set WORKER_BASE and SHORTEN_SECRET in config.py.")
        return None

    items = []
    for media_key, filename in entries:
        it = resolve_item(client, media_key, email)
        if not it:
            warn(f"Album: skipping unresolved item {filename}")
            continue
        items.append({
            "url": it["remote_url"],
            "suffix": it["suffix"],
            "filename": filename,
            "type": it["type"],
            "size": it["size"],
        })
    if not items:
        warn("Album skipped: no items could be resolved.")
        return None

    try:
        r = requests.post(
            f"{base}/album",
            json={"name": name, "items": items, "secret": secret},
            timeout=30,
        )
        if r.status_code != 200:
            warn(f"Worker /album returned HTTP {r.status_code}.")
            return None
        aid = r.json().get("id")
    except Exception as e:
        warn(f"Worker request failed: {e}")
        return None
    if not aid:
        warn("Worker did not return an album id.")
        return None
    return f"{base}/a/{aid}"


def google_ddl(client, media_key: str) -> str | None:
    """Google's own temporary direct-download URL for an item.

    Streams the original file straight from Google's CDN with no auth (the
    token is in the URL), already tagged Content-Disposition: attachment.
    The link EXPIRES after a few hours, unlike the Worker link.
    """
    try:
        d = client.api.get_download_urls(media_key)
    except Exception as e:
        warn(f"DDL skipped: {e}")
        return None
    # ["1"]["5"]["2"]["6"] = original file, ["5"] = edited version (if any).
    node = d
    for k in ("1", "5", "2"):
        node = node.get(k) if isinstance(node, dict) else None
    if not isinstance(node, dict):
        warn("DDL skipped: unexpected response shape.")
        return None
    url = node.get("6") or node.get("5")
    if not url:
        warn("DDL skipped: no download URL in response.")
        return None
    return url


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
    ap.add_argument("--album", metavar="NAME",
                    help='add uploads to a Google Photos album named NAME '
                         '(use "AUTO" to make one album per parent folder)')
    ap.add_argument("--share-album", metavar="NAME", dest="share_album",
                    help="create one public, download-only album page (Worker) for "
                         "the whole batch and print its shareable link")
    ap.add_argument("--threads", type=int, default=4, help="parallel upload threads (default: 4)")
    ap.add_argument("--no-progress", action="store_true", help="hide the gpmc progress bar")
    ap.add_argument("--links", action="store_true",
                    help="also create a permanent public download link via the Cloudflare Worker")
    ap.add_argument("--ddl", action="store_true",
                    help="also print Google's direct-download link (fast, but expires in hours)")
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
    # --share-album also creates the Google Photos album of the same name.
    gp_album = args.album or args.share_album
    if gp_album:
        info(f"Album   : {gp_album}")
    print("-" * 60)

    # Upload the whole batch in a single call so gpmc shows one combined
    # progress bar for all files instead of one bar per file.
    email = account_email(auth)
    try:
        result = client.upload(
            target=targets,
            album_name=gp_album,
            show_progress=not args.no_progress,
            threads=args.threads,
        )
    except Exception as e:
        die(f"Upload failed: {e}")

    # gpmc's progress bar leaves the cursor mid-line; break to a fresh line.
    if not args.no_progress:
        print()
    print("-" * 60)

    ok_count = 0
    fail_count = 0
    album_entries: list[tuple[str, str]] = []  # (media_key, filename) for --share-album
    for idx, path in enumerate(targets, 1):
        name = os.path.basename(path)
        key = _key_for_path(result, path)
        if not key:
            fail(f"{name}: no media key returned")
            fail_count += 1
            continue

        print(f"[{idx}/{total}] {name}")
        print(f"        photos : https://photos.google.com/photo/{key}")
        if args.links:
            dl = worker_link(client, key, email, name)
            if dl:
                print(f"        link   : {dl}")
        if args.ddl:
            g = google_ddl(client, key)
            if g:
                print(f"        ddl    : {g}")
        album_entries.append((key, name))
        ok_count += 1

    print("-" * 60)
    if args.share_album and album_entries:
        url = worker_album(client, args.share_album, album_entries, email)
        if url:
            info(f'Album "{args.share_album}" ({len(album_entries)} files):')
            print(f"    {url}")
            print("-" * 60)
    info(f"Done. {ok_count} uploaded, {fail_count} failed.")
    sys.exit(1 if fail_count else 0)


if __name__ == "__main__":
    main()
