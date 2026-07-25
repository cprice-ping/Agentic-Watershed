/**
 * OnlyPans image Worker
 * ---------------------
 * Serves photos out of contributors' own ATProto repos, cached at Cloudflare's
 * edge. Custom-lexicon blobs get none of the CDN treatment that cdn.bsky.app
 * gives app.bsky.* records, so this fills that gap: without it, every photo in
 * a gallery is a full-size original pulled live from whichever PDS its owner
 * happens to use.
 *
 * Two routes:
 *
 *   /raw/{did}/{cid}          Origin. Allowlist check, R2 cache, PDS fetch.
 *   /img/{did}:{cid}/{w}      Resized. Subrequests /raw and applies Image
 *                             Resizing on the way through.
 *
 * They're split so /img can subrequest /raw without recursing into itself.
 *
 * The nice property this whole design leans on: blob CIDs are content hashes.
 * A given URL can never mean different bytes than it did before, so everything
 * is immutable, the ETag is just the CID, and cache invalidation — normally the
 * hard half of running a CDN — is not a problem that exists here.
 */

const PLC_DIRECTORY = "https://plc.directory";

// Cache DID -> PDS for a day. Rotating a PDS is rare, and a stale entry
// self-corrects on the next miss.
const PDS_CACHE_TTL_SECONDS = 86400;

// Fixed rungs rather than arbitrary ?w= values. An open resize parameter
// fragments the cache and hands anyone a cheap way to run up Image Resizing
// billing by requesting a thousand distinct widths of the same photo.
const ALLOWED_WIDTHS = [200, 400, 800, 1600];

const IMMUTABLE = "public, max-age=31536000, immutable";

// Serving user-uploaded bytes: never let a browser sniff its way to treating
// one as a document, and give it nothing to execute if it tries.
const SAFETY_HEADERS = {
  "x-content-type-options": "nosniff",
  "content-security-policy": "default-src 'none'; sandbox",
  "cross-origin-resource-policy": "cross-origin",
};

const DID_RE = /^did:plc:[a-z2-7]{24}$/;
const CID_RE = /^[a-zA-Z0-9]{40,80}$/;

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return err(405, "method not allowed");
    }

    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter(Boolean);

    try {
      if (parts[0] === "raw") return await serveRaw(parts.slice(1), request, env, ctx);
      if (parts[0] === "img") return await serveResized(parts.slice(1), request, env, url);
      if (parts[0] === "health") return new Response("ok\n", { headers: { "cache-control": "no-store" } });
      return err(404, "not found");
    } catch (e) {
      console.error("unhandled:", e && e.stack ? e.stack : String(e));
      return err(500, "internal error");
    }
  },
};

/* ------------------------------------------------------------------ *
 * /raw/{did}/{cid} — the origin
 * ------------------------------------------------------------------ */

async function serveRaw(segments, request, env, ctx) {
  const [did, cid] = segments;

  if (!did || !cid) return err(400, "expected /raw/{did}/{cid}");
  if (!DID_RE.test(did)) return err(400, "malformed did");
  if (!CID_RE.test(cid)) return err(400, "malformed cid");

  // The allowlist is what keeps this from being an open proxy against every
  // PDS on the network. The ingest consumer writes a key here for each blob it
  // sees referenced by an indexed pan record, and deletes it when the last
  // record referencing it goes away. No key, no fetch — so this Worker can
  // only ever serve photos that are actually part of the site.
  const allowed = await env.BLOBS.get(`${did}/${cid}`, { type: "json" });
  if (!allowed) return err(404, "not in index");

  const contentType = allowed.mimeType || "application/octet-stream";

  // CID is the content hash, so it is already a perfect ETag.
  const etag = `"${cid}"`;
  if (request.headers.get("if-none-match") === etag) {
    return new Response(null, { status: 304, headers: { etag, "cache-control": IMMUTABLE } });
  }

  const key = `${did}/${cid}`;
  let object = await env.PANS.get(key);

  if (!object) {
    const pds = await resolvePds(did, env);
    if (!pds) return err(502, "could not resolve pds");

    const upstream = await fetch(
      `${pds}/xrpc/com.atproto.sync.getBlob?did=${encodeURIComponent(did)}&cid=${encodeURIComponent(cid)}`,
      { headers: { accept: "*/*" } }
    );

    if (!upstream.ok) {
      // A 404 here usually means the owner deleted the blob on their PDS but
      // ingest hasn't caught the delete yet. Short negative cache so a since-
      // repaired index doesn't stay broken for long.
      return err(upstream.status === 404 ? 404 : 502, "upstream fetch failed", {
        "cache-control": "public, max-age=60",
      });
    }

    const body = await upstream.arrayBuffer();

    // Store after responding — the visitor waiting on this request shouldn't
    // also wait on the R2 write.
    ctx.waitUntil(
      env.PANS.put(key, body, {
        httpMetadata: { contentType, cacheControl: IMMUTABLE },
      })
    );

    return new Response(request.method === "HEAD" ? null : body, {
      headers: {
        "content-type": contentType,
        "cache-control": IMMUTABLE,
        etag,
        "x-onlypans-source": "pds",
        ...SAFETY_HEADERS,
      },
    });
  }

  return new Response(request.method === "HEAD" ? null : object.body, {
    headers: {
      "content-type": object.httpMetadata?.contentType || contentType,
      "cache-control": IMMUTABLE,
      etag,
      "x-onlypans-source": "r2",
      ...SAFETY_HEADERS,
    },
  });
}

/* ------------------------------------------------------------------ *
 * /img/{did}:{cid}/{width} — resized
 * ------------------------------------------------------------------ */

async function serveResized(segments, request, env, url) {
  const [pair, widthRaw] = segments;
  if (!pair) return err(400, "expected /img/{did}:{cid}/{width}");

  // did already contains colons, so split off the last one to get the cid.
  const idx = pair.lastIndexOf(":");
  if (idx < 0) return err(400, "expected {did}:{cid}");
  const did = pair.slice(0, idx);
  const cid = pair.slice(idx + 1);

  const width = parseInt(widthRaw ?? "400", 10);
  if (!ALLOWED_WIDTHS.includes(width)) {
    return err(400, `width must be one of ${ALLOWED_WIDTHS.join(", ")}`);
  }

  const origin = new URL(url);
  origin.pathname = `/raw/${did}/${cid}`;
  origin.search = "";

  const subHeaders = new Headers({
    accept: request.headers.get("accept") || "image/*",
  });
  const inm = request.headers.get("if-none-match");
  if (inm) subHeaders.set("if-none-match", inm);

  // If Image Resizing isn't enabled on the zone, cf.image is ignored and this
  // degrades to serving the original — slower, but not broken.
  const resized = await fetch(origin.toString(), {
    cf: {
      image: {
        width,
        fit: "scale-down",
        format: "auto",     // AVIF/WebP by Accept header
        quality: 85,
        metadata: "none",   // strip EXIF — camera GPS should not ship with a photo of a saucepan
      },
    },
    headers: subHeaders,
  });

  if (!resized.ok && resized.status !== 304) return resized;

  const headers = new Headers(resized.headers);
  headers.set("cache-control", IMMUTABLE);
  // Same bytes can be AVIF or WebP depending on the request's Accept header.
  headers.set("vary", "accept");
  for (const [k, v] of Object.entries(SAFETY_HEADERS)) headers.set(k, v);

  // 304 and HEAD must both be bodyless — constructing a Response with a body
  // at status 304 throws in the Workers runtime.
  const bodyless = resized.status === 304 || request.method === "HEAD";

  return new Response(bodyless ? null : resized.body, {
    status: resized.status,
    headers,
  });
}

/* ------------------------------------------------------------------ *
 * DID resolution
 * ------------------------------------------------------------------ */

/**
 * Resolve a did:plc to its PDS endpoint, cached in KV.
 *
 * Same per-DID resolution the Synthesis subscriber does, and for the same
 * reason: contributors are not assumed to share a PDS. Someone on bsky.social
 * and someone on a self-hosted PDS both work, with no global host setting to
 * get wrong.
 */
async function resolvePds(did, env) {
  const cacheKey = `pds:${did}`;

  const cached = await env.BLOBS.get(cacheKey);
  if (cached) return cached;

  const resp = await fetch(`${PLC_DIRECTORY}/${encodeURIComponent(did)}`, {
    headers: { accept: "application/json" },
    cf: { cacheTtl: 3600, cacheEverything: true },
  });
  if (!resp.ok) return null;

  const doc = await resp.json();
  const svc = (doc.service || []).find((s) => s.id === "#atproto_pds");
  if (!svc?.serviceEndpoint) return null;

  // The DID document is third-party data naming a host we're about to fetch
  // from. Require https so a hostile or compromised document can't point this
  // at a plaintext or internal endpoint.
  let endpoint;
  try {
    endpoint = new URL(svc.serviceEndpoint);
  } catch {
    return null;
  }
  if (endpoint.protocol !== "https:") return null;

  const value = endpoint.origin;
  await env.BLOBS.put(cacheKey, value, { expirationTtl: PDS_CACHE_TTL_SECONDS });
  return value;
}

/* ------------------------------------------------------------------ */

function err(status, message, extraHeaders = {}) {
  return new Response(`${message}\n`, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "cache-control": "no-store",
      ...extraHeaders,
    },
  });
}
