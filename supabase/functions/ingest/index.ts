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
  if (!dev) return json({ error: "invalid token" }, 401);

  // 제거 신호: uninstaller가 데이터 삭제 전에 보냄. uninstalled_at 스탬프 + 자동 revoke.
  // (revoked 여부와 무관하게 처리 → 그 머신만 자기 자신을 제거 표시.)
  if (body?.uninstall === true) {
    await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${dev.id}`, {
      method: "PATCH",
      headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
      body: JSON.stringify({ uninstalled_at: new Date().toISOString(), revoked: true }),
    });
    return json({ ok: true, uninstalled: true });
  }

  if (dev.revoked) return json({ error: "revoked token" }, 401);

  // E2EE: owner 공개키 조회(비밀 아님). 컬렉터가 이걸로 per-device DEK를 봉인한다.
  // 관리자가 아직 웹에서 키를 설정하지 않았으면 null → 컬렉터는 적재를 보류(평문 전송 안 함).
  let enc: { public_key: string; kid: number } | null = null;
  {
    const kResp = await fetch(
      `${SUPABASE_URL}/rest/v1/owner_keys?select=public_key,kid&owner_id=eq.${dev.owner_id}&limit=1`,
      { headers: svcHeaders() },
    );
    if (kResp.ok) {
      const krows = await kResp.json();
      const k = Array.isArray(krows) ? krows[0] : null;
      if (k && k.public_key) enc = { public_key: k.public_key, kid: k.kid ?? 1 };
    }
  }

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
      // PostgREST 상태코드를 그대로 전달: 4xx(데이터/제약=poison) vs 5xx(일시적). 컬렉터가 구분 처리.
      const code = iResp.status >= 400 && iResp.status < 600 ? iResp.status : 502;
      return json({ error: "insert failed", detail }, code);
    }
  }

  // 하트비트: devices 갱신
  const m = body?.machine ?? {};
  const patch: Record<string, unknown> = {
    last_seen: new Date().toISOString(),
    machine_id: m.hostname ?? null,
    platform: m.platform ?? null,
    collector_version: m.version ?? null,
    last_error: m.last_error ?? null,
    last_error_at: m.last_error ? new Date().toISOString() : null,
  };
  // E2EE: 컬렉터가 보낸 봉인된 per-device DEK(owner 공개키로 wrap)를 저장. 평문 DEK는 안 받음.
  if (typeof m.wrapped_dek === "string" && m.wrapped_dek) {
    patch.wrapped_dek = m.wrapped_dek;
    patch.dek_kid = m.dek_kid ?? 1;
  }
  await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${dev.id}`, {
    method: "PATCH",
    headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
    body: JSON.stringify(patch),
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

  return json({ ok: true, inserted: events.length, backfill, enc });
});
