/**
 * Google Photos download proxy - Cloudflare Worker.
 *
 * Google serves an uploaded item's URL only to an authenticated request; a
 * public fetch gets HTTP 403. This Worker holds your gpmc credential as a
 * secret, mints a Google bearer token itself, and streams the original bytes
 * over a clean public URL.
 *
 * Endpoints:
 *   POST /shorten   { "url": "<remote_url>", "secret": "<SHORTEN_SECRET>",
 *                     "filename": "name.ext" }   ->  { "id": "<shortid>" }
 *   GET  /<id>/<filename>   -> streams the file (Content-Disposition attachment)
 *
 *   POST /album     { "name": "Show S01", "secret": "<SHORTEN_SECRET>",
 *                     "items": [{ "url", "suffix", "filename", "type", "size" }] }
 *                                  ->  { "id": "<albumid>" }
 *   GET  /a/<id>                 -> HTML portal listing every item (download-only)
 *   GET  /a/<id>/<index>/<name>  -> streams that item
 *
 * Bindings (see wrangler.toml / secrets):
 *   LINKS           KV namespace (stores id -> remote_url and the cached token)
 *   GP_AUTH_DATA    secret: the gpmc auth_data string (androidId=...&Token=...)
 *   SHORTEN_SECRET  secret: shared secret required to create links
 */

const AUTH_FIELDS = [
  "androidId", "app", "client_sig", "callerPkg", "callerSig",
  "device_country", "Email", "google_play_services_version", "lang",
  "oauth2_foreground", "sdk_version", "service",
];
const PASS_HEADERS = [
  "content-type", "content-length", "content-range",
  "accept-ranges", "etag", "last-modified",
];
const TOKEN_CACHE_KEY = "__bearer__";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/^\/+/, "");

    // Create a short link.
    if (request.method === "POST" && path === "shorten") {
      return handleShorten(request, env);
    }

    // Create an album.
    if (request.method === "POST" && path === "album") {
      return handleCreateAlbum(request, env);
    }

    // Landing page.
    if (!path) {
      return new Response(
        "Google Photos download proxy. Usage: GET /<id>/<filename>\n",
        { status: 200, headers: { "content-type": "text/plain" } },
      );
    }

    if (request.method === "GET" || request.method === "HEAD") {
      // Album routes: /a/<id> (portal) and /a/<id>/<index>/<name> (item).
      const parts = path.split("/");
      if (parts[0] === "a") {
        return handleAlbum(request, env, parts);
      }
      // Download: /<id>/<optional filename>
      return handleDownload(request, env, path);
    }

    return new Response("method not allowed", { status: 405 });
  },
};

async function handleShorten(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  if (!env.SHORTEN_SECRET || body.secret !== env.SHORTEN_SECRET) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!body.url || typeof body.url !== "string") {
    return json({ error: "missing url" }, 400);
  }
  const id = randomId();
  const record = JSON.stringify({
    url: body.url,
    filename: body.filename || "",
    suffix: typeof body.suffix === "string" ? body.suffix : "",
  });
  const opts = {};
  if (body.ttl && Number.isFinite(body.ttl)) opts.expirationTtl = Math.max(60, body.ttl);
  await env.LINKS.put("l:" + id, record, opts);
  return json({ id });
}

async function handleDownload(request, env, path) {
  const parts = path.split("/");
  const id = parts[0];
  const record = await env.LINKS.get("l:" + id, { type: "json" });
  if (!record || !record.url) {
    return new Response("not found", { status: 404 });
  }
  const filename = decodeURIComponent(parts.slice(1).join("/")) || record.filename || id;
  return streamItem(env, request, {
    url: record.url,
    suffix: record.suffix,
    filename,
  });
}

// Fetch one item from Google (authenticated) and stream it to the client as a
// download. `item` is { url, suffix, filename }. Shared by the single-link and
// album routes.
async function streamItem(env, request, item) {
  // suffix picks the download variant: =d for photos, =dv for videos.
  const suffix = item.suffix || (item.url.includes("=") ? "" : "=d");
  const src = item.url + suffix;

  let resp = await fetchAuthed(env, src, request, false);
  // If the cached token was stale, mint a fresh one and retry once.
  if (resp.status === 401 || resp.status === 403) {
    resp = await fetchAuthed(env, src, request, true);
  }
  if (resp.status !== 200 && resp.status !== 206) {
    return new Response(`upstream error ${resp.status}`, { status: 502 });
  }

  const filename = item.filename || "download";
  const headers = new Headers();
  for (const h of PASS_HEADERS) {
    const v = resp.headers.get(h);
    if (v) headers.set(h, v);
  }
  headers.set("Content-Disposition", `attachment; filename="${filename.replace(/"/g, "")}"`);
  headers.set("Access-Control-Allow-Origin", "*");

  return new Response(request.method === "HEAD" ? null : resp.body, {
    status: resp.status,
    headers,
  });
}

async function handleCreateAlbum(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }
  if (!env.SHORTEN_SECRET || body.secret !== env.SHORTEN_SECRET) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!Array.isArray(body.items) || body.items.length === 0) {
    return json({ error: "missing items" }, 400);
  }
  // Keep only the fields we need, and drop anything without a url.
  const items = [];
  for (const it of body.items) {
    if (it && typeof it.url === "string" && it.url) {
      items.push({
        url: it.url,
        suffix: typeof it.suffix === "string" ? it.suffix : "",
        filename: typeof it.filename === "string" ? it.filename : "",
        type: it.type,
        size: Number.isFinite(it.size) ? it.size : undefined,
      });
    }
  }
  if (items.length === 0) {
    return json({ error: "no valid items" }, 400);
  }
  const id = randomId();
  const record = JSON.stringify({
    name: typeof body.name === "string" ? body.name : "",
    created: Date.now(),
    items,
  });
  const opts = {};
  if (body.ttl && Number.isFinite(body.ttl)) opts.expirationTtl = Math.max(60, body.ttl);
  await env.LINKS.put("a:" + id, record, opts);
  return json({ id });
}

async function handleAlbum(request, env, parts) {
  const id = parts[1];
  if (!id) return new Response("not found", { status: 404 });
  const record = await env.LINKS.get("a:" + id, { type: "json" });
  if (!record || !Array.isArray(record.items)) {
    return new Response("not found", { status: 404 });
  }

  // /a/<id>/<index>/<name> -> stream that item.
  if (parts.length >= 3) {
    const index = parseInt(parts[2], 10);
    const item = Number.isInteger(index) ? record.items[index] : null;
    if (!item) return new Response("not found", { status: 404 });
    return streamItem(env, request, item);
  }

  // /a/<id> -> portal page.
  return renderAlbumPage(id, record);
}

function renderAlbumPage(id, record) {
  const title = record.name || "Shared album";
  const rows = record.items.map((it, i) => {
    const name = it.filename || `file-${i + 1}`;
    const href = `/a/${encodeURIComponent(id)}/${i}/${encodeURIComponent(name)}`;
    const size = it.size ? formatBytes(it.size) : "";
    return `      <li class="row">
        <span class="name">${esc(name)}</span>
        <span class="size">${esc(size)}</span>
        <a class="dl" href="${esc(href)}" download>Download</a>
      </li>`;
  }).join("\n");

  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(title)}</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         background: #f6f7f9; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) { body { background: #14161a; color: #e6e6e6; } }
  .wrap { max-width: 820px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { opacity: .65; font-size: 13px; margin-bottom: 20px; }
  .bar { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
  button.all { border: 0; border-radius: 8px; padding: 9px 16px; font-size: 14px;
               background: #2f6fed; color: #fff; cursor: pointer; }
  button.all:disabled { opacity: .5; cursor: default; }
  ul { list-style: none; margin: 0; padding: 0; border-radius: 10px; overflow: hidden;
       border: 1px solid rgba(128,128,128,.25); }
  .row { display: flex; align-items: center; gap: 12px; padding: 11px 14px;
         border-top: 1px solid rgba(128,128,128,.18); background: rgba(128,128,128,.04); }
  .row:first-child { border-top: 0; }
  .name { flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
          white-space: nowrap; }
  .size { flex: 0 0 auto; opacity: .6; font-variant-numeric: tabular-nums; font-size: 13px; }
  a.dl { flex: 0 0 auto; text-decoration: none; color: #2f6fed; font-weight: 600; }
  a.dl:hover { text-decoration: underline; }
  .note { margin-top: 18px; font-size: 12.5px; opacity: .6; }
</style>
</head>
<body>
  <div class="wrap">
    <h1>${esc(title)}</h1>
    <div class="meta">${record.items.length} file${record.items.length === 1 ? "" : "s"}</div>
    <div class="bar">
      <button class="all" id="all">Download all</button>
    </div>
    <ul>
${rows}
    </ul>
    <p class="note">Downloads stream directly from Google Photos. "Download all"
      starts each file in sequence; your browser may ask to allow multiple downloads.</p>
  </div>
  <script>
    document.getElementById("all").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      const links = Array.from(document.querySelectorAll("a.dl"));
      for (const link of links) {
        const a = document.createElement("a");
        a.href = link.href;
        a.download = "";
        document.body.appendChild(a);
        a.click();
        a.remove();
        await new Promise((r) => setTimeout(r, 1200));
      }
      btn.disabled = false;
    });
  </script>
</body>
</html>`;

  return new Response(html, {
    status: 200,
    headers: { "content-type": "text/html; charset=utf-8" },
  });
}

function esc(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function formatBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
}

async function fetchAuthed(env, src, request, forceFresh) {
  const bearer = await getBearer(env, forceFresh);
  const fwd = new Headers();
  fwd.set("Authorization", "Bearer " + bearer);
  const range = request.headers.get("Range");
  if (range) fwd.set("Range", range);
  return fetch(src, { headers: fwd });
}

async function getBearer(env, forceFresh) {
  if (!forceFresh) {
    const cached = await env.LINKS.get(TOKEN_CACHE_KEY, { type: "json" });
    const now = Math.floor(Date.now() / 1000);
    if (cached && cached.auth && cached.exp > now + 60) return cached.auth;
  }

  const d = Object.fromEntries(new URLSearchParams(env.GP_AUTH_DATA));
  const form = new URLSearchParams();
  for (const k of AUTH_FIELDS) if (d[k] != null) form.set(k, d[k]);
  form.set("Token", d["Token"]);

  const r = await fetch("https://android.googleapis.com/auth", {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded",
      "app": "com.google.android.apps.photos",
      "device": d["androidId"] || "",
      "User-Agent": "GoogleAuth/1.4 (Pixel XL PQ2A.190205.001); gzip",
    },
    body: form.toString(),
  });
  const text = await r.text();
  const kv = {};
  for (const line of text.split("\n")) {
    const i = line.indexOf("=");
    if (i > 0) kv[line.slice(0, i)] = line.slice(i + 1);
  }
  if (!kv["Auth"]) throw new Error("token mint failed: " + (kv["Error"] || r.status));

  const exp = parseInt(kv["Expiry"] || "0", 10) || (Math.floor(Date.now() / 1000) + 3600);
  await env.LINKS.put(
    TOKEN_CACHE_KEY,
    JSON.stringify({ auth: kv["Auth"], exp }),
    { expirationTtl: 3600 },
  );
  return kv["Auth"];
}

function randomId(len = 6) {
  const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789";
  const bytes = new Uint8Array(len);
  crypto.getRandomValues(bytes);
  let out = "";
  for (const b of bytes) out += alphabet[b % alphabet.length];
  return out;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
