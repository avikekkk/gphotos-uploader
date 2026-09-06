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

    // Landing page.
    if (!path) {
      return new Response(
        "Google Photos download proxy. Usage: GET /<id>/<filename>\n",
        { status: 200, headers: { "content-type": "text/plain" } },
      );
    }

    // Download: /<id>/<optional filename>
    if (request.method === "GET" || request.method === "HEAD") {
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
  // suffix picks the download variant: =d for photos, =dv for videos.
  const suffix = record.suffix || (record.url.includes("=") ? "" : "=d");
  const src = record.url + suffix;

  let resp = await fetchAuthed(env, src, request, false);
  // If the cached token was stale, mint a fresh one and retry once.
  if (resp.status === 401 || resp.status === 403) {
    resp = await fetchAuthed(env, src, request, true);
  }
  if (resp.status !== 200 && resp.status !== 206) {
    return new Response(`upstream error ${resp.status}`, { status: 502 });
  }

  const headers = new Headers();
  for (const h of PASS_HEADERS) {
    const v = resp.headers.get(h);
    if (v) headers.set(h, v);
  }
  headers.set("Content-Disposition", `attachment; filename="${filename.replace(/"/g, "")}"`);
  headers.set("Access-Control-Allow-Origin", "*");

  const method = request.method;
  return new Response(method === "HEAD" ? null : resp.body, {
    status: resp.status,
    headers,
  });
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
