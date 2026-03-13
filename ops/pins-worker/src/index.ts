export interface Env {
  DB: D1Database;
  OPS_PINS_TOKEN: string;
}

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function utcTs(): string {
  return new Date().toISOString().replace(".000Z", "Z");
}

function unauthorized(): Response {
  return json({ ok: false, error: "unauthorized" }, 401);
}

function requireToken(req: Request, env: Env): boolean {
  const expected = (env.OPS_PINS_TOKEN || "").trim();
  if (!expected) return true; // allow open if unset (same pattern as dashboard)
  const got = (req.headers.get("X-Ops-Token") || "").trim();
  return got === expected;
}

function route(pathname: string): { kind: string; id?: string } {
  // /api/pins
  if (pathname === "/api/pins") return { kind: "pins" };

  // /api/pins/:id/pin
  const m1 = pathname.match(/^\/api\/pins\/([^/]+)\/pin$/);
  if (m1) return { kind: "pin-pin", id: decodeURIComponent(m1[1]) };

  // /api/pins/:id
  const m2 = pathname.match(/^\/api\/pins\/([^/]+)$/);
  if (m2) return { kind: "pin", id: decodeURIComponent(m2[1]) };

  return { kind: "notfound" };
}

async function readJson(req: Request): Promise<any> {
  try {
    return await req.json();
  } catch {
    return {};
  }
}

function newId(): string {
  // 16 hex chars, similar to the local dashboard pins.
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const r = route(url.pathname);

    if (r.kind === "notfound") {
      return json({ ok: false, error: "not found" }, 404);
    }

    if (!requireToken(req, env)) {
      return unauthorized();
    }

    if (r.kind === "pins") {
      if (req.method === "GET") {
        const { results } = await env.DB.prepare(
          "SELECT id, title, body, pinned, created_at FROM pins ORDER BY pinned DESC, created_at DESC"
        ).all();

        const pins = (results || []).map((row: any) => ({
          id: String(row.id),
          title: String(row.title || ""),
          body: String(row.body || ""),
          pinned: Boolean(row.pinned),
          created_at: String(row.created_at || ""),
        }));

        return json({ ok: true, ts: utcTs(), pins });
      }

      if (req.method === "POST") {
        const d = await readJson(req);
        const title = String(d.title || "").trim();
        const body = String(d.body || "").trim();
        if (!title && !body) {
          return json({ ok: false, error: "missing title/body" }, 400);
        }

        const id = newId();
        const created_at = utcTs();
        await env.DB.prepare(
          "INSERT INTO pins (id, title, body, pinned, created_at) VALUES (?, ?, ?, 1, ?)"
        )
          .bind(id, title, body, created_at)
          .run();

        return json({ ok: true, id });
      }

      return json({ ok: false, error: "method not allowed" }, 405);
    }

    if (r.kind === "pin-pin") {
      if (req.method !== "POST") {
        return json({ ok: false, error: "method not allowed" }, 405);
      }
      const d = await readJson(req);
      const pinned = Boolean(d.pinned);

      const res = await env.DB.prepare("UPDATE pins SET pinned=? WHERE id=?")
        .bind(pinned ? 1 : 0, r.id)
        .run();

      if ((res.meta?.changes || 0) < 1) {
        return json({ ok: false, error: "not found" }, 404);
      }
      return json({ ok: true });
    }

    if (r.kind === "pin") {
      if (req.method !== "DELETE") {
        return json({ ok: false, error: "method not allowed" }, 405);
      }

      const res = await env.DB.prepare("DELETE FROM pins WHERE id=?").bind(r.id).run();
      if ((res.meta?.changes || 0) < 1) {
        return json({ ok: false, error: "not found" }, 404);
      }
      return json({ ok: true });
    }

    return json({ ok: false, error: "not found" }, 404);
  },
};
