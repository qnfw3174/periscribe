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
  const mguid = (body?.machine?.machine_guid ?? "").toString();

  // 1) 토큰으로 발급 시 생성된 디바이스 행 조회
  const dResp = await fetch(
    `${SUPABASE_URL}/rest/v1/devices?select=id,owner_id,revoked,machine_guid,dek_keys&token_hash=eq.${encodeURIComponent(hash)}&limit=1`,
    { headers: svcHeaders() },
  );
  if (!dResp.ok) return json({ error: "device lookup failed" }, 502);
  const trows = await dResp.json();
  const tokenDev = Array.isArray(trows) ? trows[0] : null;
  if (!tokenDev) return json({ error: "invalid token" }, 401);

  // 2) 디바이스 연속성: machine_guid로 정체성 확정. 같은 머신(guid)에 기존 행이 있으면
  //    재설치로 보고 그 행에 토큰을 repoint하고 토큰행은 버린다 → 관리자는 1개로 이어서 관리.
  let dev = tokenDev;
  if (mguid) {
    const cResp = await fetch(
      `${SUPABASE_URL}/rest/v1/devices?select=id,owner_id,revoked,dek_keys&owner_id=eq.${tokenDev.owner_id}&machine_guid=eq.${encodeURIComponent(mguid)}&limit=1`,
      { headers: svcHeaders() },
    );
    const crows = cResp.ok ? await cResp.json() : [];
    const existing = Array.isArray(crows) ? crows[0] : null;
    if (existing && existing.id !== tokenDev.id) {
      // revoke된 머신은 재설치해도 되살리지 않음(의도적 차단 유지).
      if (existing.revoked) return json({ error: "revoked token" }, 401);
      // 갓 발급된 토큰행 삭제 후, 기존 행에 새 토큰을 연결(uninstalled 표시 해제).
      await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${tokenDev.id}`,
        { method: "DELETE", headers: svcHeaders({ Prefer: "return=minimal" }) });
      await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${existing.id}`, {
        method: "PATCH",
        headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
        body: JSON.stringify({ token_hash: hash, uninstalled_at: null }),
      });
      dev = existing;
    } else if (tokenDev.machine_guid !== mguid) {
      // 최초 바인딩: 토큰행에 machine_guid 기록.
      await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${tokenDev.id}`, {
        method: "PATCH",
        headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
        body: JSON.stringify({ machine_guid: mguid }),
      });
    }
  }

  // 제거 신호: uninstaller가 데이터 삭제 전에 보냄. uninstalled_at 스탬프 + 자동 revoke.
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
  if (mguid) patch.machine_guid = mguid;
  // E2EE: 봉인된 per-device DEK를 dek_keys[kid]에 "누적"(덮어쓰기 X) → 재설치로 새 세대가 생겨도
  // 옛 세대 DEK가 남아 옛 로그 복호 유지. wrapped_dek/dek_kid는 현재 세대 포인터로 함께 갱신.
  if (typeof m.wrapped_dek === "string" && m.wrapped_dek && m.dek_kid != null) {
    const keys = (dev.dek_keys && typeof dev.dek_keys === "object") ? { ...dev.dek_keys } : {};
    keys[String(m.dek_kid)] = m.wrapped_dek;
    patch.dek_keys = keys;
    patch.wrapped_dek = m.wrapped_dek;
    patch.dek_kid = m.dek_kid;
  }
  await fetch(`${SUPABASE_URL}/rest/v1/devices?id=eq.${dev.id}`, {
    method: "PATCH",
    headers: svcHeaders({ "Content-Type": "application/json", Prefer: "return=minimal" }),
    body: JSON.stringify(patch),
  });

  // 세션 카탈로그: 컬렉터가 보고한 로컬 세션 목록을 upsert(내용 미적재 세션도 웹 목록에 표시용).
  const catalog = Array.isArray(body?.catalog) ? body.catalog : [];
  if (catalog.length > 0) {
    const now = new Date().toISOString();
    const crows = catalog
      .filter((c: any) => c && c.session_id)
      .map((c: any) => ({
        owner_id: dev.owner_id, device_id: dev.id, session_id: String(c.session_id),
        project: c.project ?? null, container_id: c.container_id ?? null,
        file_mtime: c.mtime ?? null, size_bytes: c.size ?? null, updated_at: now,
      }));
    if (crows.length > 0) {
      await fetch(`${SUPABASE_URL}/rest/v1/session_catalog?on_conflict=device_id,session_id`, {
        method: "POST",
        headers: svcHeaders({ "Content-Type": "application/json", Prefer: "resolution=merge-duplicates,return=minimal" }),
        body: JSON.stringify(crows),
      });
    }
  }

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
