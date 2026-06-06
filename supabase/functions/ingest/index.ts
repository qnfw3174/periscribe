// Periscribe ingest — 디바이스 토큰으로 인증하는 적재 게이트웨이.
// 각 PC의 Collector는 service_role/anon 키 없이 자기 device_token만 보유한다.
// 흐름: device_token sha256 → devices 조회(revoked=false) → owner 확정 →
//        events 에 owner_id/device_id 스탬프하여 service_role로 insert(멱등) →
//        devices.last_seen 갱신(하트비트). verify_jwt=false (커스텀 인증).
import "jsr:@supabase/functions-js/edge-runtime.d.ts";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

function svcHeaders(extra: Record<string, string> = {}) {
  return { apikey: SERVICE_KEY, Authorization: `Bearer ${SERVICE_KEY}`, ...extra };
}
function json(obj: unknown, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json" } });
}
async function sha256hex(s: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

Deno.serve(async (req: Request) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);

  let body: any;
  try { body = await req.json(); } catch { return json({ error: "invalid json" }, 400); }

  const token = (body?.device_token ?? "").toString();
  if (!token) return json({ error: "missing device_token" }, 401);
  const hash = await sha256hex(token);

  // 디바이스 조회
  const dResp = await fetch(
    `${SUPABASE_URL}/rest/v1/devices?select=id,owner_id,revoked&token_hash=eq.${encodeURIComponent(hash)}&limit=1`,
    { headers: svcHeaders() },
  );
  if (!dResp.ok) return json({ error: "device lookup failed" }, 502);
  const rows = await dResp.json();
  const dev = Array.isArray(rows) ? rows[0] : null;
  if (!dev || dev.revoked) return json({ error: "invalid or revoked token" }, 401);

  // events 적재 (owner_id/device_id 스탬프). 멱등 upsert(중복 무시).
  const events = Array.isArray(body?.events) ? body.events : [];
  if (events.length > 0) {
    const stamped = events.map((e: any) => ({ ...e, owner_id: dev.owner_id, device_id: dev.id }));
    const iResp = await fetch(`${SUPABASE_URL}/rest/v1/events?on_conflict=event_id`, {
      method: "POST",
      headers: svcHeaders({ "Content-Type": "application/json", Prefer: "resolution=ignore-duplicates,return=minimal" }),
      body: JSON.stringify(stamped),
    });
    if (!iResp.ok) {
      const detail = await iResp.text();
      return json({ error: "insert failed", detail }, 502);
    }
  }

  // 하트비트: devices 갱신
  const m = body?.machine ?? {};
  await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${dev.id}`, {
    method: "PATCH",
    headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
    body: JSON.stringify({
      last_seen: new Date().toISOString(),
      machine_id: m.hostname ?? null,
      platform: m.platform ?? null,
      collector_version: m.version ?? null,
    }),
  });

  // 백필 요청 픽업: 이 디바이스의 pending 요청을 가져와 done 처리하고 session_id 목록을 반환.
  // 컬렉터는 이 목록의 로컬 transcript 파일을 처음부터 재적재(멱등)한다.
  let backfill: string[] = [];
  try {
    const bResp = await fetch(
      `${SUPABASE_URL}/rest/v1/backfill_requests?select=id,session_id&device_id=eq.${dev.id}&status=eq.pending`,
      { headers: svcHeaders() },
    );
    if (bResp.ok) {
      const reqs = await bResp.json();
      if (Array.isArray(reqs) && reqs.length > 0) {
        backfill = reqs.map((r: any) => r.session_id).filter(Boolean);
        const ids = reqs.map((r: any) => r.id).join(",");
        await fetch(`${SUPABASE_URL}/rest/v1/backfill_requests?id=in.(${ids})`, {
          method: "PATCH",
          headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
          body: JSON.stringify({ status: "done", done_at: new Date().toISOString() }),
        });
      }
    }
  } catch (_) { /* 백필 픽업 실패는 적재 성공을 막지 않음 */ }

  return json({ ok: true, inserted: events.length, backfill });
});
