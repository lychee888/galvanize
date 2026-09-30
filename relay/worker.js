/*
 * galvanize relay — Cloudflare Worker (template, user-deployed)
 *
 * Zero-inbound-port webhook ingress for laptops: any service POSTs here,
 * the galvanize daemon long-polls GET /events?since= and dispatches.
 *
 * Deploy (the user owns it — no vendor in the middle):
 *   1. wrangler new -> replace worker.js with this file
 *   2. wrangler kv namespace create RELAY
 *   3. set secrets: wrangler secret put RELAY_TOKEN   (queue reads)
 *                   wrangler secret put INGEST_TOKEN  (submissions)
 *   4. wrangler deploy
 *   5. galvanize add webhook --name mysvc --wake dsh --relay https://<name>.<sub>.workers.dev --relay-token <RELAY_TOKEN>
 *   6. senders POST with Authorization: Bearer <INGEST_TOKEN>
 *
 * Storage: KV, key = zero-padded timestamp+random, value = event JSON.
 * Events expire after TTL_DAYS. Pollers page with the `since` cursor.
 */
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") || "";
    const ok = token && env.RELAY_TOKEN && token === env.RELAY_TOKEN;

    // CORS for browser-based senders
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders(request) });
    }

    // POST /ingest/<route>  — external services (GitHub/Stripe/etc.) call this.
    // Route path is echoed back so the daemon knows which trigger to fire.
    if (request.method === "POST" && url.pathname.startsWith("/ingest/")) {
      // Separate ingestion credentials from the credential used to read mail.
      const ingestToken = env.INGEST_TOKEN;
      if (!ingestToken) return json(503, { error: "INGEST_TOKEN is not configured" });
      if (token !== ingestToken) return json(401, { error: "unauthorized" });
      const route = url.pathname.slice("/ingest/".length).replace(/[^a-z0-9_-]/g, "");
      if (!route) return json(400, { error: "route required" });
      let body = await request.text();
      if (body.length > 200000) return json(413, { error: "payload too large" });
      // Normalize non-object JSON before queueing; readers also handle old entries.
      try {
        const parsed = JSON.parse(body);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          body = JSON.stringify({ value: parsed });
        }
      } catch { body = JSON.stringify({ raw: body }); }
      // Senders must provide Authorization: Bearer <INGEST_TOKEN>.
      const key = String(Date.now()).padStart(14, "0") + "-" + crypto.randomUUID().slice(0, 8);
      await env.RELAY.put(key, JSON.stringify({ route, body, ts: Date.now() }),
                          { expirationTtl: 60 * 60 * 24 * (env.TTL_DAYS || 7) });
      return json(200, { status: "queued", id: key });
    }

    // GET /events?since=<cursor>&limit=N — authenticated poll (the daemon only)
    if (request.method === "GET" && url.pathname === "/events") {
      if (!ok) return json(401, { error: "unauthorized" });
      const since = url.searchParams.get("since") || "0";
      const limit = Math.min(parseInt(url.searchParams.get("limit") || "50"), 200);
      // KV list() paginates server-side; scan sorted keys after `since`
      const events = [];
      let cursor;
      do {
        const list = await env.RELAY.list(cursor ? { cursor } : {});
        for (const k of list.keys) {
          if (k.name <= since) continue;
          const v = await env.RELAY.get(k.name);
          if (v) events.push({ id: k.name, route: undefined, ...safeParse(v) });
          if (events.length >= limit) break;
        }
        cursor = events.length >= limit ? undefined : list.cursor;
      } while (cursor && events.length < limit);
      const next = events.length ? events[events.length - 1].id : since;
      return json(200, { events, since: next });
    }

    if (url.pathname === "/health") return json(200, { status: "ok" });
    return json(404, { error: "not found" });
  },
};

function json(status, obj) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", "access-control-allow-origin": "*" },
  });
}
function safeParse(v) {
  try { return JSON.parse(v); } catch { return { body: v }; }
}
function corsHeaders(request) {
  return {
    "access-control-allow-origin": request.headers.get("origin") || "*",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "access-control-allow-headers": "authorization,content-type",
  };
}
