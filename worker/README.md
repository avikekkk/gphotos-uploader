# Cloudflare Worker: Google Photos download proxy

Turns a private Google Photos upload into a clean, public download link.

Google serves an uploaded item's URL only to an authenticated request (a public
fetch gets HTTP 403). This Worker holds your gpmc credential as a secret, mints
a Google bearer token itself, and streams the original bytes over a public
`workers.dev` URL. No Google login on the downloader's side.

## Endpoints

- `POST /shorten` `{ "url": "<remote_url>", "secret": "<SHORTEN_SECRET>", "filename": "name.ext" }`
  -> `{ "id": "<shortid>" }`  (called by `up.py --links`)
- `GET /<id>/<filename>` -> streams the file as a download.

## Deploy (one time, ~5 minutes)

You need a free Cloudflare account and Node installed.

```bash
cd worker

# 1. Log in (opens a browser)
npx wrangler login

# 2. Create the KV namespace, then paste the printed id into wrangler.toml
#    (replace PUT_YOUR_KV_NAMESPACE_ID_HERE)
npx wrangler kv namespace create LINKS

# 3. Store your secrets (never commit these)
#    Paste your gpmc auth_data string when prompted:
npx wrangler secret put GP_AUTH_DATA
#    Choose any strong shared secret (used by up.py):
npx wrangler secret put SHORTEN_SECRET

# 4. Deploy
npx wrangler deploy
```

`wrangler deploy` prints your Worker URL, e.g.
`https://gphotos-proxy.<your-subdomain>.workers.dev`.

## Wire it into the uploader

In the parent folder's `config.py` set:

```python
WORKER_BASE = "https://gphotos-proxy.<your-subdomain>.workers.dev"
SHORTEN_SECRET = "<the same secret you put above>"
```

Then:

```bash
uv run up.py --links --file video.mp4
```

You get a `link :` line with a public download URL.

## Get your auth_data string

If you don't have it handy:

```bash
uv run get_auth_data.py "<oauth_token>"
```

Paste that string when `wrangler secret put GP_AUTH_DATA` prompts.

## Notes

- The Worker caches the bearer token in KV for ~1 hour and re-mints it on
  expiry or a 403, so downloads keep working without per-request auth calls.
- `GP_AUTH_DATA` in the Worker is full access to that Google account. Keep it a
  Worker secret; never put it in `wrangler.toml`.
- Anyone with a `/<id>/<filename>` link can download that one item. The ids are
  random and unguessable. Rotate `SHORTEN_SECRET` to cut off new link creation.
- Local test (optional): create `worker/.dev.vars` with `GP_AUTH_DATA=...` and
  `SHORTEN_SECRET=...`, then `npx wrangler dev --local`. `.dev.vars` is
  gitignored.
