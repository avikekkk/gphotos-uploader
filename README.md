# Google Photos Uploader + public links

Upload files to Google Photos from the command line, and optionally get a clean
public download link via a Cloudflare Worker. No bot, no server to babysit.

## Files

- `up.py`            - the uploader
- `upload.sh`        - quick wrapper that keeps the progress bar
- `get_auth_data.py` - one-time: make your credential from an oauth_token
- `config.py`        - your account credential + Worker settings
- `worker/`          - the Cloudflare Worker that serves public download links

## Upload (with uv - installs deps automatically)

```bash
uv run up.py --file video.mp4
uv run up.py a.mp4 b.jpg c.mkv
uv run up.py --dir ./clips
```

or with the wrapper (progress bar, runs from anywhere):

```bash
./upload.sh video.mp4
```

Each upload prints the Google Photos link. Your credential is already in
`config.py`.

## Public download links (optional)

Google Photos URLs are private - a public fetch gets HTTP 403. To hand someone
a working download link, deploy the Worker in `worker/` once. It holds your
credential and streams the file over a public `workers.dev` URL.

1. Deploy it: see `worker/README.md` (about 5 minutes, free Cloudflare account).
2. Put its URL and secret in `config.py`:
   ```python
   WORKER_BASE = "https://gphotos-proxy.<you>.workers.dev"
   SHORTEN_SECRET = "<your secret>"
   ```
3. Add `--links`:
   ```bash
   uv run up.py --links --file video.mp4
   ```
   Output:
   ```
   [1/1] video.mp4 (1.6 GB)
           photos : https://photos.google.com/photo/<key>
           link   : https://gphotos-proxy.<you>.workers.dev/ab12cd/video.mp4
   ```
   The `link` is downloadable by anyone, no Google login.

## Share a whole season/collection (one album link)

Handing out one `--links` URL per episode is tedious. `--share-album NAME`
uploads the batch and prints a **single public link** to a download-only portal
page that lists every file with its own Download button (plus "Download all"):

```bash
uv run up.py --dir ./show-s01 --share-album "Show S01"
```

Output ends with:

```
[*] Album "Show S01" (10 files):
    https://gphotos-proxy.<you>.workers.dev/a/ab12cd
```

Anyone with that link sees the list and can download each file (streamed from
Google, no login). `--share-album` also creates a Google Photos album of the
same name. It's download-only - no in-browser player - and needs the Worker
deployed, same as `--links`.

## Auth

Already set in `config.py`. To use another account, regenerate it:

```bash
uv run get_auth_data.py "<oauth_token from accounts.google.com/EmbeddedSetup>"
```

Paste the printed string into `config.py` as `ANDROID_CREDS` (and into the
Worker as the `GP_AUTH_DATA` secret if you use links).

## Notes

- `config.py` holds a secret (your account credential) and is gitignored.
- `--links` needs the Worker deployed and `WORKER_BASE` / `SHORTEN_SECRET` set.
  Plain uploads need neither.
- How links work: `up.py` uploads, reads the item's Google remote URL from
  gpmc's local library cache, registers it with the Worker's `/shorten`, and
  prints `WORKER_BASE/<id>/<filename>`. The Worker authenticates to Google on
  each download and streams the original bytes.

## First-run setup (fresh clone)

1. `cp config.py.example config.py`
2. Generate your credential: `uv run get_auth_data.py "<oauth_token>"` and paste
   the result into `config.py` as `ANDROID_CREDS`.
3. For public links, deploy the Worker (`worker/README.md`) and set
   `WORKER_BASE` / `SHORTEN_SECRET` in `config.py`.

## Credits

- **[gpmc](https://github.com/xob0t/gpmc)** by [xob0t](https://github.com/xob0t)
  - the Google Photos mobile client that powers uploads and the library sync.
- **[gotohp](https://github.com/xob0t/gotohp)** by
  [xob0t](https://github.com/xob0t) - the oauth_token -> auth_data exchange in
  `get_auth_data.py` is ported from its Go implementation.
- Original uploader/proxy scripts and the idea shared by a friend.
- Uploader CLI, the Cloudflare Worker proxy, and packaging assembled with the
  help of Claude Code.
