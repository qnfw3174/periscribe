/* Periscribe Web UI
 * Supabase 에 직접 붙어 과거 이벤트를 조회(REST)하고 신규 INSERT 를 Realtime 으로 받는다.
 * 커스텀 백엔드 없음. anon(read-only) 키만 사용.
 */
(function () {
  "use strict";

  const cfg = window.PERISCRIBE_CONFIG || {};
  const feedEl = document.getElementById("feed");
  const emptyEl = document.getElementById("empty");
  const countEl = document.getElementById("count");
  const connDot = document.getElementById("conn-dot");
  const connText = document.getElementById("conn-text");

  const F = {
    machine: document.getElementById("f-machine"),
    session: document.getElementById("f-session"),
    kind: document.getElementById("f-kind"),
    severity: document.getElementById("f-severity"),
    category: document.getElementById("f-category"),
    container: document.getElementById("f-container"),
    text: document.getElementById("f-text"),
    errors: document.getElementById("f-errors"),
  };

  // 로깅 출처 탭: all | transcript | api | os-exec. ev.source 로 분류
  // (transcript = "claude-code" 또는 미지정, api = "api", os = "os-exec").
  let tabSource = "all";
  function srcGroup(ev) {
    if (ev.source === "api") return "api";
    if (ev.source === "os-exec") return "os-exec";
    return "transcript";
  }

  // 메모리 상의 이벤트(event_id -> event). 멱등: 중복 INSERT 무시.
  const store = new Map();
  const TRUNC = 1200; // 표시용 절단 길이(저장은 전문, 절단은 UI 단에서만)

  if (!cfg.SUPABASE_URL || !cfg.SUPABASE_URL.startsWith("http") || !cfg.SUPABASE_ANON_KEY) {
    emptyEl.textContent = "config.js 를 만들고 SUPABASE_URL / SUPABASE_ANON_KEY 를 채우세요 (config.example.js 참고).";
    return;
  }

  const table = cfg.TABLE || "events";
  const pageSize = cfg.PAGE_SIZE || 200;
  const client = window.supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY, {
    realtime: { params: { eventsPerSecond: 20 } },
  });

  // ---------- E2EE (종단간 암호화) ----------
  // 관리자 RSA 키쌍(개인키는 패스프레이즈로 봉인되어 서버 보관, 평문은 브라우저에서만 복원).
  // per-device DEK(AES-256)는 owner 공개키로 봉인되어 devices.wrapped_dek 에 저장됨.
  // 운영자/DB는 암호문만 본다. 메타데이터는 평문(필터/인덱스용).
  const KDF_ITERS = 600000;
  const dekCache = new Map();    // device_id -> CryptoKey(AES-GCM)
  let ownerPrivKey = null;       // CryptoKey (RSA-OAEP private)
  let ownerKeysRow = null;       // owner_keys 행

  function bufToB64(buf) {
    const b = new Uint8Array(buf); let s = "";
    for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s);
  }
  function b64ToBuf(s) {
    const bin = atob(s); const u = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
    return u.buffer;
  }
  async function deriveKEK(passphrase, saltBuf, iters) {
    const base = await crypto.subtle.importKey(
      "raw", new TextEncoder().encode(passphrase), "PBKDF2", false, ["deriveKey"]);
    return crypto.subtle.deriveKey(
      { name: "PBKDF2", salt: saltBuf, iterations: iters, hash: "SHA-256" },
      base, { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }
  async function aesWrap(kek, dataBuf) {
    const n = crypto.getRandomValues(new Uint8Array(12));
    const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv: n }, kek, dataBuf);
    return { v: 1, n: bufToB64(n), ct: bufToB64(ct) };
  }
  async function aesUnwrap(kek, env) {
    return crypto.subtle.decrypt(
      { name: "AES-GCM", iv: new Uint8Array(b64ToBuf(env.n)) }, kek, b64ToBuf(env.ct));
  }
  async function genOwnerKeypair() {
    return crypto.subtle.generateKey(
      { name: "RSA-OAEP", modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
      true, ["encrypt", "decrypt"]);
  }
  async function exportPubSPKI(pub) { return bufToB64(await crypto.subtle.exportKey("spki", pub)); }
  async function wrapPriv(kek, priv) { return aesWrap(kek, await crypto.subtle.exportKey("pkcs8", priv)); }
  async function importPriv(pkcs8Buf) {
    return crypto.subtle.importKey("pkcs8", pkcs8Buf, { name: "RSA-OAEP", hash: "SHA-256" }, true, ["decrypt"]);
  }
  async function unwrapPriv(kek, env) { return importPriv(await aesUnwrap(kek, env)); }

  // 이 디바이스의 특정 세대(kid) DEK 복원(개인키로 RSA-OAEP unwrap → AES 키). (device,kid)별 캐시.
  // 재설치로 DEK 세대가 늘어도 dek_keys[kid]에서 해당 세대를 찾아 옛 로그까지 복호.
  async function dekForDevice(deviceId, kid) {
    if (!deviceId || !ownerPrivKey) return null;
    const ck = deviceId + ":" + kid;
    if (dekCache.has(ck)) return dekCache.get(ck);
    const d = deviceMap.get(deviceId);
    if (!d) return null;
    const wrapped = (d.dek_keys && d.dek_keys[String(kid)]) || d.wrapped_dek; // 폴백: 단일 wrapped_dek
    if (!wrapped) return null;
    try {
      const raw = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, ownerPrivKey, b64ToBuf(wrapped));
      const key = await crypto.subtle.importKey("raw", raw, { name: "AES-GCM" }, false, ["decrypt"]);
      dekCache.set(ck, key);
      return key;
    } catch (e) { return null; }
  }
  async function decField(dek, env) {
    const pt = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: new Uint8Array(b64ToBuf(env.n)) }, dek, b64ToBuf(env.ct));
    return JSON.parse(new TextDecoder().decode(pt));
  }
  // 암호화 행을 제자리 복호화(payload/raw 평문 객체로 치환). 실패 시 _encLocked.
  async function decryptEvent(ev) {
    if (!ev || ev.enc_version !== 1 || ev._dec) return ev;
    const kid = (ev.payload && ev.payload.kid) || (ev.raw && ev.raw.kid) || ev.dek_kid || 1;
    const dek = await dekForDevice(ev.device_id, kid);
    if (!dek) { ev._encLocked = true; return ev; }
    try {
      if (ev.payload && ev.payload.ct) ev.payload = await decField(dek, ev.payload);
      if (ev.raw && ev.raw.ct) ev.raw = await decField(dek, ev.raw);
      ev._dec = true; ev._encLocked = false;
      // 잠긴 상태로 분류(info)됐을 수 있으니 메모이즈 해제 → 복호 후 재분류.
      delete ev._sev; delete ev._cat;
    } catch (e) { ev._encLocked = true; }
    return ev;
  }
  async function decryptRows(rows) {
    for (const ev of (rows || [])) await decryptEvent(ev);
    return rows;
  }

  // ---------- 유틸 ----------
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function fmtTime(ev) {
    const t = ev.ts || ev.received_at;
    if (!t) return "";
    const d = new Date(t);
    if (isNaN(d)) return "";
    return d.toLocaleTimeString([], { hour12: false }) + "." +
      String(d.getMilliseconds()).padStart(3, "0");
  }
  function eventTime(ev) {
    // 실제 발생 시각(ts)을 진짜 시간순 기준으로 사용. 백필돼도 세션 진행 순서가 유지된다.
    // (received_at은 Collector 수신 시각이라 백필 시 한꺼번에 찍혀 순서가 뭉개짐.)
    // ts가 없을 때만 received_at으로 폴백. 문자열이 아니라 ms로 변환해 정확히 비교.
    const ms = Date.parse(ev.ts || ev.received_at || "");
    return isNaN(ms) ? 0 : ms;
  }
  function cmpEvents(a, b) {
    const d = eventTime(a) - eventTime(b);
    if (d !== 0) return d;
    // 동시각(같은 줄의 멀티블록 등): event_id로 안정 정렬(uuid#0 < uuid#1 → 블록 순서 보존).
    const ai = a.event_id || "", bi = b.event_id || "";
    return ai < bi ? -1 : ai > bi ? 1 : 0;
  }

  // ---------- 렌더 ----------
  function payloadText(ev) {
    if (ev.enc_version === 1 && !ev._dec) {
      return ev._encLocked ? "🔒 복호화 불가 (이 디바이스 키 없음/손상)" : "🔒 잠김 — 잠금 해제 필요";
    }
    const p = ev.payload || {};
    if (ev.kind === "tool_result") return p.output_full || "";
    if (ev.kind === "user_prompt" || ev.kind === "assistant_text") return p.text || "";
    if (ev.kind === "tool_use") {
      if (ev.tool === "Bash") return p.command || "";
      try { return JSON.stringify(p.input != null ? p.input : p, null, 2); }
      catch (e) { return String(p); }
    }
    if (ev.kind === "process_exec") return p.command_line || "";   // OS exec 감사(쉘 명령)
    try { return JSON.stringify(p, null, 2); } catch (e) { return String(p); }
  }

  function bodyHtml(ev) {
    const raw = payloadText(ev);
    const isLong = raw.length > TRUNC;
    const shown = isLong ? raw.slice(0, TRUNC) : raw;
    const more = isLong ? `<span class="truncated" data-full="1">▾ ${raw.length - TRUNC}자 더보기</span>` : "";

    if (ev.kind === "tool_use" && ev.tool === "Bash") {
      const bg = (ev.payload && ev.payload.run_in_background) ? ` <span class="badge-bg">[background]</span>` : "";
      const desc = ev.payload && ev.payload.description ? `<div class="event__meta">${esc(ev.payload.description)}</div>` : "";
      return desc + `<pre class="code bash">${esc(shown)}</pre>${bg}${more}`;
    }
    if (ev.kind === "user_prompt" || ev.kind === "assistant_text") {
      return `<div class="text-body">${esc(shown)}</div>${more}`;
    }
    if (ev.kind === "process_exec") {
      const p = ev.payload || {};
      const meta = [];
      if (p.image) meta.push("🐚 " + esc(String(p.image).split(/[\\/]/).pop()));
      if (p.user) meta.push(esc(p.user));
      if (p.parent_image) meta.push("← " + esc(String(p.parent_image).split(/[\\/]/).pop()));
      const metaLine = meta.length ? `<div class="event__meta">${meta.join(" · ")}</div>` : "";
      return metaLine + `<pre class="code bash">${esc(shown)}</pre>${more}`;
    }
    return `<pre class="code">${esc(shown)}</pre>${more}`;
  }

  // ---------- 크리티컬 작업 분류 ----------
  const SEV_RANK = { info: 1, warning: 2, critical: 3 };

  // 민감 파일: 자격증명 계열(critical) / 설정 계열(warning)
  const SENSITIVE_CRED_RE =
    /\.env\b|\.ssh\b|id_rsa|id_ed25519|[\/\\]credentials\b|\.pem\b|\.npmrc\b|\.pypirc\b|\.aws[\/\\]|\bsecrets?\b/i;
  const SENSITIVE_CONFIG_RE =
    /settings\.json|\.github[\/\\]workflows|[\/\\]hooks[\/\\]|dockerfile|\.gitconfig|\bcrontab\b/i;

  // 순서 있는 규칙(critical 먼저). 명령 문자열에 첫 매치가 심각도를 결정.
  const BASE_RULES = [
    // critical — 파괴적 파일작업
    { re: /\brm\s+-\w*[rf]/i, severity: "critical", category: "destructive" },
    { re: /remove-item\b[^\n]*-(recurse|force)\b/i, severity: "critical", category: "destructive" },
    { re: /\bdel\s+\/[a-z]|\brmdir\s+\/s|\bformat\s+[a-z]:|\bmkfs\b|\bdd\s+if=|>\s*\/dev\/sd/i, severity: "critical", category: "destructive" },
    // critical — 권한/승격
    { re: /\bsudo\b|\bchmod\b|\bchown\b|\bicacls\b|\brunas\b|set-executionpolicy/i, severity: "critical", category: "privilege" },
    // critical — 자격증명 접근
    { re: /\.env\b|\.ssh\b|id_rsa|id_ed25519|\.aws[\/\\]credentials|\.pem\b|gh\s+auth\s+token|get-credential|password\s*=/i, severity: "critical", category: "credential" },
    // critical — 원격코드/네트워크 유출
    { re: /\b(curl|wget)\b[^\n]*\|\s*(sh|bash|zsh)|(iwr|invoke-webrequest|curl)\b[^\n]*\|\s*iex|\bnc\b\s+-|\bnetcat\b|\bncat\b|\bssh\s+\S+@|\bscp\b/i, severity: "critical", category: "network" },
    // critical — 레지스트리/서비스/방화벽
    { re: /\breg\s+(add|delete)\b|\bHK(LM|CU|EY_)|\bsc\s+(create|delete|stop|config)\b|stop-service|new-service|set-service|netsh\s+advfirewall/i, severity: "critical", category: "registry" },
    // critical — VCS 위험
    { re: /git\s+push\b[^\n]*(--force|-f)\b|git\s+reset\s+--hard|git\s+clean\s+-\w*f|git\s+checkout\s+--\s|git\s+update-ref\s+-d|repo\s+delete/i, severity: "critical", category: "vcs" },
    // critical — 인프라/DB 변경
    { re: /\bdrop\s+(table|database|schema)\b|\btruncate\s+table\b|\bdelete\s+from\b|kubectl\s+delete|terraform\s+(apply|destroy)|aws\s+[a-z0-9-]+\s+(delete|terminate|rm)\b/i, severity: "critical", category: "infra" },

    // warning — 패키지 설치(공급망)
    { re: /\b(npm|pnpm|yarn)\s+(i|install|add)\b|\bpip3?\s+install\b|\bpipx\s+install|winget\s+install|choco\s+install|\bapt(-get)?\s+install|\bgem\s+install|cargo\s+install|go\s+install|brew\s+install/i, severity: "warning", category: "package" },
    // warning — 프로세스 종료
    { re: /\bkill\b|\bpkill\b|\btaskkill\b|stop-process/i, severity: "warning", category: "process" },
    // warning — 환경변수 변경
    { re: /\bexport\s+\w+=|\bsetx\b|set-item\s+env:|\$env:\w+\s*=/i, severity: "warning", category: "env" },
  ];

  // config.js 에서 EXTRA_RULES 로 규칙을 앞에 덧붙일 수 있음(코드 수정 없이 환경별 확장)
  const RULES = ((cfg.EXTRA_RULES || []).map((r) => ({
    re: r.re instanceof RegExp ? r.re : new RegExp(r.re, "i"),
    severity: r.severity, category: r.category,
  }))).concat(BASE_RULES);

  function matchRules(text) {
    if (!text) return null;
    for (const r of RULES) if (r.re.test(text)) return r;
    return null;
  }

  // 분류 입력 문자열(명령 또는 파일경로) 추출
  function commandTextOf(ev) {
    const p = ev.payload || {};
    if (p.command) return p.command;
    if (p.command_line) return p.command_line;     // OS exec(쉘 명령)도 같은 룰엔진 통과
    if (p.input) { try { return JSON.stringify(p.input); } catch (e) { return String(p.input); } }
    return "";
  }

  // tool_use 1건을 분류(입력 불변 → 이벤트에 메모이즈)
  function classifyAction(ev) {
    if (ev._sev) return { severity: ev._sev, category: ev._cat };
    let severity = "info", category = "";
    if (ev.kind === "tool_use") {
      const tool = ev.tool || "";
      const fp = (ev.payload && ev.payload.input && ev.payload.input.file_path) || "";
      if (tool === "Edit" || tool === "Write" || tool === "NotebookEdit") {
        if (SENSITIVE_CRED_RE.test(fp)) { severity = "critical"; category = "credential"; }
        else if (SENSITIVE_CONFIG_RE.test(fp)) { severity = "warning"; category = "config"; }
      } else if (tool === "Read" || tool === "Glob" || tool === "Grep") {
        if (SENSITIVE_CRED_RE.test(fp)) { severity = "critical"; category = "credential"; }
      } else {
        const hit = matchRules(commandTextOf(ev));
        if (hit) { severity = hit.severity; category = hit.category; }
      }
    } else if (ev.kind === "process_exec") {
      const hit = matchRules(commandTextOf(ev));   // OS exec 쉘 명령에도 위험도 룰 적용(재사용)
      if (hit) { severity = hit.severity; category = hit.category; }
    }
    ev._sev = severity; ev._cat = category;
    return { severity, category };
  }

  function pairedToolUse(ev) {
    if (!ev.tool_use_id) return null;
    for (const o of store.values())
      if (o.kind === "tool_use" && o.tool_use_id === ev.tool_use_id) return o;
    return null;
  }

  // 표시/필터에 쓰는 심각도·카테고리(tool_result는 짝 tool_use 상속)
  function severityOf(ev) {
    if (ev.kind === "tool_result") {
      const pair = pairedToolUse(ev);
      if (pair) return classifyAction(pair).severity;
      return ev.is_error ? "warning" : "info";
    }
    if (ev.kind === "tool_use" || ev.kind === "process_exec") return classifyAction(ev).severity;
    return "info";
  }
  function categoryOf(ev) {
    if (ev.kind === "tool_result") {
      const pair = pairedToolUse(ev);
      return pair ? classifyAction(pair).category : "";
    }
    if (ev.kind === "tool_use" || ev.kind === "process_exec") return classifyAction(ev).category;
    return "";
  }

  function eventNode(ev) {
    const el = document.createElement("article");
    const isBash = ev.kind === "tool_use" && ev.tool === "Bash";
    const isErr = ev.kind === "tool_result" && ev.is_error === true;
    const sev = severityOf(ev);
    const cat = categoryOf(ev);
    el.className = "event event--" + ev.kind +
      (isBash ? " is-bash" : "") + (isErr ? " is-error" : "") +
      (sev === "critical" ? " is-critical" : "");
    el.dataset.eventId = ev.event_id;
    el.dataset.full = payloadText(ev);

    const toolBadge = ev.tool
      ? `<span class="tool-badge ${isBash ? "bash" : ""}">${esc(ev.tool)}</span>` : "";
    const sevBadge = sev === "critical"
      ? `<span class="sev-badge critical" title="critical">● critical</span>`
      : sev === "warning"
      ? `<span class="sev-badge warning" title="warning">● warning</span>` : "";
    const catChip = cat ? `<span class="cat-chip">${esc(cat)}</span>` : "";
    const ctrBadge = ev.container_id
      ? `<span class="container-badge" title="container: ${esc(ev.container_id)}">🐳 ${esc(ev.container_id)}</span>` : "";
    const srcBadge = ev.source === "os-exec"
      ? `<span class="src-badge" title="Claude 프로세스 트리(OS 레벨) — transcript가 못 보는 하위 프로세스까지">🐚 Claude OS</span>`
      : ev.source === "api"
      ? `<span class="src-badge" title="Anthropic API 트래픽(외부 관찰자) — transcript 비의존, 로컬 게이트웨이 프록시">🛰 API</span>` : "";
    const sidechain = ev.is_sidechain ? `<span class="sidechain">↳ sub</span>` : "";
    const sess = ev.session_id ? esc(ev.session_id).slice(0, 8) : "";

    el.innerHTML =
      `<div class="event__head">
         <span class="event__kind kind--${ev.kind}">${esc(ev.kind)}</span>
         ${toolBadge}${sevBadge}${catChip}${ctrBadge}${srcBadge}
         <span class="event__meta">${esc(ev.machine_id || "")} · ${sess}</span>
         ${sidechain}
         <span class="event__time">${fmtTime(ev)}</span>
       </div>
       <div class="event__body">${bodyHtml(ev)}</div>`;
    return el;
  }

  // 클릭으로 전문 펼치기(표시 절단 해제)
  feedEl.addEventListener("click", function (e) {
    const t = e.target;
    if (t.classList && t.classList.contains("truncated")) {
      const art = t.closest(".event");
      if (!art) return;
      const full = art.dataset.full || "";
      const body = art.querySelector(".event__body");
      const pre = body.querySelector("pre.code");
      const div = body.querySelector(".text-body");
      if (pre) pre.textContent = full;
      else if (div) div.textContent = full;
      t.remove();
    }
  });

  // ---------- 필터 ----------
  function passesFilter(ev) {
    if (tabSource !== "all" && srcGroup(ev) !== tabSource) return false;
    if (F.machine.value && ev.machine_id !== F.machine.value) return false;
    if (F.session.value && ev.session_id !== F.session.value) return false;
    if (F.kind.value && ev.kind !== F.kind.value) return false;
    if (F.errors.checked && !(ev.kind === "tool_result" && ev.is_error === true)) return false;
    if (F.severity.value) {
      const s = severityOf(ev);
      if (F.severity.value === "critical" && s !== "critical") return false;
      if (F.severity.value === "warn" && SEV_RANK[s] < 2) return false;
    }
    if (F.category.value && categoryOf(ev) !== F.category.value) return false;
    const q = F.text.value.trim().toLowerCase();
    if (q) {
      const hay = (payloadText(ev) + " " + (ev.tool || "")).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  }

  // 카테고리는 클라이언트 분류 결과라 로드된 이벤트에서 채운다.
  // 머신/세션은 DB 전체에서 채운다(loadFilterOptions) → 로드 안 된 과거 세션도 선택 가능.
  function refreshFilterOptions() {
    const cats = new Set();
    for (const ev of store.values()) {
      const c = categoryOf(ev);
      if (c) cats.add(c);
    }
    syncSelect(F.category, cats);
  }

  const knownSessions = new Set();
  // 세션 목록의 최근 수정시간 표기. 오늘이면 HH:mm, 아니면 MM-DD HH:mm.
  function fmtListTime(ms) {
    const d = new Date(ms);
    if (isNaN(d)) return "";
    const p2 = (n) => String(n).padStart(2, "0");
    const hm = `${p2(d.getHours())}:${p2(d.getMinutes())}`;
    const now = new Date();
    const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    return sameDay ? hm : `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${hm}`;
  }
  function shortProject(p) {
    if (!p) return "";
    const seg = String(p).split(/[\/\\]/).filter(Boolean).pop() || p;
    return seg.length > 20 ? seg.slice(0, 20) + "…" : seg;
  }
  // 세션 행 병합: session_catalog(로컬 전체, 미적재 포함) ∪ sessions 뷰(적재 건수). 최근 변경순.
  function buildSessionRows(sess, cat) {
    const ingested = new Map();             // session_id -> sessions 뷰 행
    const machineSet = new Set(), containerSet = new Set();
    for (const s of sess) {
      ingested.set(s.session_id, s);
      knownSessions.add(s.session_id);
      if (s.machine_id) machineSet.add(s.machine_id);
      if (s.container_id) containerSet.add(s.container_id);
    }
    const merged = new Map();               // session_id -> 표시 행
    const add = (sid, project, container_id, count, sortTs) => {
      const prev = merged.get(sid);
      const row = { session_id: sid, project, container_id, count, sort: sortTs };
      if (!prev || row.sort > prev.sort) merged.set(sid, Object.assign(prev || {}, row));
    };
    for (const c of cat) {
      const ing = ingested.get(c.session_id);
      if (c.container_id) containerSet.add(c.container_id);
      add(c.session_id, (ing && ing.project) || c.project, (ing && ing.container_id) || c.container_id,
          ing ? ing.event_count : 0,
          Date.parse((ing && ing.last_received) || c.file_mtime || 0) || 0);
    }
    for (const s of sess) {                 // 카탈로그에 없지만 적재된 세션(현재 진행/레거시)도 포함
      if (!merged.has(s.session_id))
        add(s.session_id, s.project, s.container_id, s.event_count, Date.parse(s.last_received || 0) || 0);
    }
    const rows = Array.from(merged.values()).sort((a, b) => b.sort - a.sort);  // 최근 변경순
    return { rows, machineSet, containerSet };
  }

  function sessionRowLabel(r) {
    const proj = r.project ? " · " + shortProject(r.project) : "";
    const ctr = r.container_id ? " · 🐳" + r.container_id : "";
    const load = r.count > 0 ? `${r.count}건` : "미적재";
    const when = r.sort ? " · " + fmtListTime(r.sort) : "";   // 최근 수정시간
    return `${String(r.session_id).slice(0, 8)} · ${load}${when}${proj}${ctr}`;
  }

  async function fetchSessionRows() {
    const [sessRes, catRes] = await Promise.all([
      client.from("sessions").select("*"),
      client.from("session_catalog").select("session_id,project,container_id,file_mtime"),
    ]);
    return buildSessionRows(sessRes.data || [], catRes.data || []);
  }

  async function loadFilterOptions() {
    const { rows, machineSet, containerSet } = await fetchSessionRows();

    const cur = F.session.value;
    while (F.session.options.length > 1) F.session.remove(1);
    for (const r of rows) {
      const o = document.createElement("option");
      o.value = r.session_id;
      o.textContent = sessionRowLabel(r);
      F.session.appendChild(o);
    }
    if ([...F.session.options].some((o) => o.value === cur)) F.session.value = cur;

    // 머신: 등록된 devices + 적재 세션의 machine_id 합집합
    for (const d of deviceMap.values()) if (d.machine_id) machineSet.add(d.machine_id);
    syncSelect(F.machine, machineSet);
    // 컨테이너: "전체" + "호스트(native)만"(__none__) + 각 container_id
    if (F.container && containerSet.size) {
      const cur2 = F.container.value;
      F.container.length = 0;
      F.container.appendChild(new Option("전체", ""));
      F.container.appendChild(new Option("🖥 호스트만", "__none__"));
      for (const c of Array.from(containerSet).sort()) F.container.appendChild(new Option("🐳 " + c, c));
      if ([...F.container.options].some((o) => o.value === cur2)) F.container.value = cur2;
    }
  }
  function syncSelect(sel, values, sliceLabel) {
    const cur = sel.value;
    const wanted = Array.from(values).sort();
    // 기존 옵션(전체 제외) 제거 후 재구성
    while (sel.options.length > 1) sel.remove(1);
    for (const v of wanted) {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = sliceLabel ? v.slice(0, sliceLabel) + "…" : v;
      sel.appendChild(o);
    }
    if (wanted.indexOf(cur) !== -1) sel.value = cur;
  }

  function countText(shown) {
    const totalStr = dbTotal != null ? ` · DB ~${dbTotal}건` : "";
    return `로드 ${store.size}건 중 ${shown}개 표시${totalStr}`;
  }

  function render(scrollBottom) {
    const list = Array.from(store.values()).filter(passesFilter);
    list.sort(cmpEvents);
    feedEl.innerHTML = "";
    if (list.length === 0) {
      feedEl.appendChild(emptyEl);
      emptyEl.textContent = store.size === 0 ? "이벤트 없음. Collector 가 도는지 확인하세요." : "필터에 맞는 이벤트 없음.";
      lastShown = 0;
      countEl.textContent = countText(0);
      return;
    }
    const frag = document.createDocumentFragment();
    for (const ev of list) frag.appendChild(eventNode(ev));
    feedEl.appendChild(frag);
    lastShown = list.length;
    countEl.textContent = countText(list.length);
    if (scrollBottom) window.scrollTo(0, document.body.scrollHeight);
  }

  // ---------- 데이터 ----------
  let lastRecv = null;   // 지금까지 본 최신 received_at (Realtime 끊김 후 따라잡기 기준)
  function upsert(ev) {
    if (!ev || !ev.event_id) return;
    store.set(ev.event_id, ev); // 멱등
    if (ev.received_at && (lastRecv == null || ev.received_at > lastRecv)) lastRecv = ev.received_at;
  }

  // ---------- 페이지네이션(과거 더 불러오기) ----------
  // 복합 키셋 커서 (received_at, event_id): received_at 동률이 있어도 누락/중복 없음.
  let curRecv = null, curId = null;
  let reachedEnd = false;    // 더 불러올 과거가 없음
  let dbTotal = null;        // DB 총 건수(추정치, 표시용)
  let lastShown = 0;         // 마지막 render에서 표시한 건수(비차단 count 갱신용)

  // 현재 서버측 필터를 적용한 기본 쿼리(최근순, pageSize 제한). received_at 동률은 event_id로 안정 정렬.
  function applyServerFilters(q) {
    if (F.machine.value) q = q.eq("machine_id", F.machine.value);
    if (F.session.value) q = q.eq("session_id", F.session.value);
    if (F.kind.value) q = q.eq("kind", F.kind.value);
    if (F.errors.checked) q = q.eq("is_error", true);
    if (F.container && F.container.value) {
      if (F.container.value === "__none__") q = q.is("container_id", null);   // 호스트 native만
      else q = q.eq("container_id", F.container.value);
    }
    return q;
  }
  function baseQuery() {
    return applyServerFilters(
      client.from(table).select("*")
        .order("received_at", { ascending: false })
        .order("event_id", { ascending: false })
        .limit(pageSize)
    );
  }

  function applyPage(rows) {
    for (const ev of rows) upsert(ev);
    if (rows.length) {
      const last = rows[rows.length - 1];
      curRecv = last.received_at; curId = last.event_id;
    }
    if (rows.length < pageSize) reachedEnd = true;
  }

  function updateLoadMore() {
    if (!loadMoreBtn) return;
    loadMoreBtn.style.display = reachedEnd ? "none" : "";
    loadMoreBtn.disabled = false;
    loadMoreBtn.textContent = "↑ 더 불러오기 (과거)";
  }

  // 세션이 선택됐을 때만 "이 세션 과거 전체 불러오기"(컬렉터 백필) 버튼을 보인다.
  function updateBackfill() {
    const b = document.getElementById("backfill-session");
    const on = !!(F.session && F.session.value);
    if (b) {
      b.style.display = on ? "" : "none";
      if (on && !b.dataset.busy) {
        b.disabled = false;
        b.textContent = "⟳ 이 세션 과거 전체 불러오기 (수집 PC)";
      }
    }
    const d = document.getElementById("delete-session");
    if (d) {
      d.style.display = on ? "" : "none";
      if (on && !d.dataset.busy) {
        d.disabled = false;
        d.textContent = "🗑 이 세션 삭제";
      }
    }
  }

  async function loadHistory() {
    setConn("off", "과거 로드 중…");
    curRecv = null; curId = null; reachedEnd = false; store.clear();
    dbTotal = null;

    // 1) 최근 pageSize(200)개만 먼저 가져와 즉시 표시(전체 count를 기다리지 않음).
    const { data, error } = await baseQuery();
    if (error) {
      setConn("err", "조회 오류: " + error.message);
      emptyEl.textContent = "조회 오류: " + error.message + " (anon 키/RLS 확인)";
      return;
    }
    await decryptRows(data || []);
    applyPage(data || []);
    refreshFilterOptions();
    render(true);
    updateLoadMore();
    updateBackfill();
    setConn("on", "실시간 구독 중");

    // 2) DB 총건수는 비차단 + 추정치(전체 풀스캔 회피). 도착하면 카운트 라벨만 갱신.
    applyServerFilters(client.from(table).select("*", { count: "estimated", head: true }))
      .then((cnt) => {
        if (cnt && typeof cnt.count === "number") {
          dbTotal = cnt.count;
          countEl.textContent = countText(lastShown);
        }
      }).catch(() => {});
  }

  async function loadMore() {
    if (reachedEnd || curRecv == null) return;
    loadMoreBtn.disabled = true;
    loadMoreBtn.textContent = "불러오는 중…";
    // received_at < cur  OR  (received_at == cur AND event_id < curId) — 동률 경계 누락 방지
    const { data, error } = await baseQuery().or(
      `received_at.lt.${curRecv},and(received_at.eq.${curRecv},event_id.lt.${curId})`
    );
    if (error) { updateLoadMore(); return; }
    await decryptRows(data || []);
    applyPage(data || []);
    refreshFilterOptions();
    render(false);  // 과거를 위로 붙이므로 스크롤 위치 유지
    updateLoadMore();
  }

  // Realtime 끊김 동안 누락된 이벤트를 따라잡는다(서버필터 적용, lastRecv 이후, 멱등 머지).
  let catchingUp = false;
  async function catchUp() {
    if (!appStarted || catchingUp) return;
    catchingUp = true;
    try {
      if (lastRecv == null) { await loadHistory(); return; }
      let q = client.from(table).select("*")
        .gte("received_at", lastRecv)   // 경계 포함(동률 누락 방지) — 멱등이라 중복 안전
        .order("received_at", { ascending: true })
        .limit(2000);
      q = applyServerFilters(q);
      const { data, error } = await q;
      if (!error && Array.isArray(data)) {
        await decryptRows(data);
        let added = 0, newSession = false;
        for (const ev of data) {
          if (!store.has(ev.event_id)) added++;
          upsert(ev);
          // catchUp 중 처음 보는 세션이면 드롭다운(세션 목록)도 갱신해야 한다(새로고침 없이 보이게).
          if (ev.session_id && !knownSessions.has(ev.session_id)) { knownSessions.add(ev.session_id); newSession = true; }
        }
        if (added) { refreshFilterOptions(); render(true); }
        if (newSession) loadFilterOptions();
      }
      await loadDevices();  // 디바이스 상태도 재동기화
    } catch (_) { /* 일시 오류는 다음 트리거에서 재시도 */ }
    finally { catchingUp = false; }
  }

  let evChannel = null, evResubTimer = null;
  function subscribe() {
    if (evChannel) { try { client.removeChannel(evChannel); } catch (_) {} evChannel = null; }
    evChannel = client.channel("periscribe-events")
      .on("postgres_changes", { event: "INSERT", schema: "public", table: table }, async (payload) => {
        await decryptEvent(payload.new);
        upsert(payload.new);
        if (dbTotal != null) dbTotal += 1;
        // 새 세션이 생기면 세션 드롭다운을 DB 전체 기준으로 갱신
        if (payload.new && payload.new.session_id && !knownSessions.has(payload.new.session_id)) {
          knownSessions.add(payload.new.session_id);
          loadFilterOptions();
        }
        refreshFilterOptions();
        render(true);  // 새 이벤트는 맨 아래 → 따라가기
      })
      .subscribe((status) => {
        if (status === "SUBSCRIBED") { setConn("on", "실시간 구독 중"); catchUp(); }  // (재)구독 시 갭 메움
        else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT" || status === "CLOSED") {
          setConn("err", "Realtime 재연결 중…");
          if (!evResubTimer) evResubTimer = setTimeout(() => { evResubTimer = null; subscribe(); }, 3000);
        }
      });
  }

  function setConn(state, text) {
    connDot.className = "dot dot--" + (state === "on" ? "on" : state === "err" ? "err" : "off");
    connText.textContent = text;
  }

  // ---------- 이벤트 바인딩 ----------
  const loadMoreBtn = document.getElementById("load-more");
  let textDebounce;
  F.text.addEventListener("input", () => {
    clearTimeout(textDebounce);
    textDebounce = setTimeout(() => render(false), 150);
  });
  // 머신/세션/종류/컨테이너/실패 = 서버측 필터 → DB에서 다시 조회(로드 안 된 세션도 가져옴).
  [F.machine, F.session, F.kind, F.container, F.errors].forEach((el) =>
    el && el.addEventListener("change", loadHistory));
  // 심각도/카테고리 = 클라이언트 분류 → 로드된 것에서 즉시 필터.
  [F.severity, F.category].forEach((el) =>
    el.addEventListener("change", () => render(false)));
  // 로깅 출처 탭 = 클라이언트 필터(이미 로드된 이벤트의 source 기준).
  const sourceTabs = document.getElementById("source-tabs");
  if (sourceTabs) sourceTabs.addEventListener("click", (e) => {
    const btn = e.target.closest(".src-tab");
    if (!btn) return;
    tabSource = btn.dataset.src || "all";
    sourceTabs.querySelectorAll(".src-tab").forEach((t) =>
      t.classList.toggle("active", t === btn));
    render(false);
  });
  document.getElementById("reload").addEventListener("click", loadHistory);
  if (loadMoreBtn) loadMoreBtn.addEventListener("click", loadMore);

  // "이 세션 과거 전체 불러오기": 백필 요청을 넣으면 수집 PC가 하트비트 때 받아 로컬 파일을
  // 처음부터 재적재한다(멱등). 채워지는 과거 이벤트는 Realtime 으로 들어와 자동 렌더.
  const backfillBtn = document.getElementById("backfill-session");
  if (backfillBtn) backfillBtn.addEventListener("click", async () => {
    const sid = F.session.value;
    if (!sid) return;
    backfillBtn.dataset.busy = "1";
    backfillBtn.disabled = true; backfillBtn.textContent = "요청 중…";
    // 이 세션을 적재할 디바이스 찾기(요청 라우팅). 적재된 세션은 events에서,
    // 미적재 세션은 session_catalog에서 device_id를 얻는다.
    let device_id = null;
    const { data: ev } = await client.from(table).select("device_id")
      .eq("session_id", sid).not("device_id", "is", null).limit(1);
    if (ev && ev[0]) device_id = ev[0].device_id;
    if (!device_id) {
      const { data: c } = await client.from("session_catalog").select("device_id")
        .eq("session_id", sid).limit(1);
      if (c && c[0]) device_id = c[0].device_id;
    }
    if (!device_id) {
      backfillBtn.disabled = false; delete backfillBtn.dataset.busy;
      backfillBtn.textContent = "수집 디바이스를 못 찾음";
      return;
    }
    const { error } = await client.from("backfill_requests")
      .insert({ owner_id: currentUserId, session_id: sid, device_id });
    if (error) {
      backfillBtn.disabled = false; delete backfillBtn.dataset.busy;
      backfillBtn.textContent = "요청 실패: " + esc(error.message);
      return;
    }
    backfillBtn.textContent = "✓ 요청됨 — 수집 PC가 온라인이면 곧 채워집니다";
    setTimeout(() => { delete backfillBtn.dataset.busy; updateBackfill(); }, 8000);
  });

  // 세션 완전 삭제: 중앙 DB(events/목록/백필) 제거 + 수집 PC의 로컬 transcript 파일까지 삭제.
  const deleteBtn = document.getElementById("delete-session");
  if (deleteBtn) deleteBtn.addEventListener("click", async () => {
    const sid = F.session.value;
    if (!sid) return;
    if (!confirm("이 세션을 영구 삭제합니다.\n\n중앙 DB의 이벤트·목록은 즉시 삭제되고, 수집 PC의 로컬 "
      + "transcript 파일은 그 PC가 온라인일 때 삭제됩니다. 되돌릴 수 없습니다. 계속할까요?")) return;
    deleteBtn.dataset.busy = "1";
    deleteBtn.disabled = true; deleteBtn.textContent = "삭제 중…";
    // events엔 직접 delete RLS가 없어 정의자 RPC로 소유 검증 후 일괄 삭제 + 로컬삭제 명령 큐잉.
    const { error } = await client.rpc("purge_session", { p_session_id: sid });
    if (error) {
      delete deleteBtn.dataset.busy;
      deleteBtn.disabled = false; deleteBtn.textContent = "삭제 실패: " + esc(error.message);
      return;
    }
    delete deleteBtn.dataset.busy;
    F.session.value = "";        // 삭제된 세션 필터 해제
    await loadFilterOptions();    // 드롭다운에서 제거
    await loadHistory();          // 피드 갱신(이 세션 사라짐)
  });

  // ---------- 세션 관리: 다중선택 일괄 삭제 ----------
  function updateSessionSelCount() {
    const n = document.querySelectorAll("#sessions-list .sess-chk:checked").length;
    const cnt = document.getElementById("sessions-selcount");
    const btn = document.getElementById("sessions-delete");
    if (cnt) cnt.textContent = n ? `${n}개 선택됨` : "";
    if (btn) btn.disabled = n === 0;
  }
  function renderSessionsList(rows) {
    const el = document.getElementById("sessions-list");
    if (!el) return;
    if (!rows.length) { el.innerHTML = '<div class="health-empty">세션이 없습니다.</div>'; return; }
    el.innerHTML = rows.map((r) =>
      `<label class="session-row"><input type="checkbox" class="sess-chk" value="${esc(r.session_id)}" />` +
      `<span class="sess-label">${esc(sessionRowLabel(r))}</span></label>`).join("");
    el.querySelectorAll(".sess-chk").forEach((c) => c.addEventListener("change", updateSessionSelCount));
    const all = document.getElementById("sessions-all"); if (all) all.checked = false;
    updateSessionSelCount();
  }
  const sessModal = document.getElementById("sessions-modal");
  const sessManageBtn = document.getElementById("manage-sessions");
  if (sessManageBtn) sessManageBtn.addEventListener("click", async () => {
    if (sessModal) sessModal.style.display = "flex";
    const el = document.getElementById("sessions-list");
    if (el) el.innerHTML = '<div class="health-empty">불러오는 중…</div>';
    renderSessionsList((await fetchSessionRows()).rows);
  });
  const sessClose = document.getElementById("sessions-close");
  if (sessClose) sessClose.addEventListener("click", () => { if (sessModal) sessModal.style.display = "none"; });
  if (sessModal) sessModal.addEventListener("click", (e) => { if (e.target === sessModal) sessModal.style.display = "none"; });
  const sessAll = document.getElementById("sessions-all");
  if (sessAll) sessAll.addEventListener("change", () => {
    document.querySelectorAll("#sessions-list .sess-chk").forEach((c) => { c.checked = sessAll.checked; });
    updateSessionSelCount();
  });
  const sessDelete = document.getElementById("sessions-delete");
  if (sessDelete) sessDelete.addEventListener("click", async () => {
    const ids = [...document.querySelectorAll("#sessions-list .sess-chk:checked")].map((c) => c.value);
    if (!ids.length) return;
    if (!confirm(`선택한 ${ids.length}개 세션을 영구 삭제합니다.\n\n중앙 DB의 이벤트·목록은 즉시 삭제되고, `
      + `수집 PC의 로컬 transcript 파일은 그 PC가 온라인일 때 삭제됩니다. 되돌릴 수 없습니다. 계속할까요?`)) return;
    sessDelete.disabled = true; sessDelete.textContent = "삭제 중…";
    const { error } = await client.rpc("purge_sessions", { p_session_ids: ids });
    sessDelete.textContent = "🗑 선택 삭제";
    if (error) { alert("삭제 실패: " + error.message); sessDelete.disabled = false; return; }
    if (ids.includes(F.session.value)) F.session.value = "";
    renderSessionsList((await fetchSessionRows()).rows);  // 모달 목록 갱신
    await loadFilterOptions();                              // 드롭다운 갱신
    await loadHistory();                                   // 피드 갱신
  });

  // ---------- 머신(디바이스) 헬스 + 관리 ----------
  const healthChips = document.getElementById("health-chips");
  const deviceMap = new Map();              // device.id -> device row
  let currentUserId = null;                 // 토큰 발급 시 owner_id
  const HEALTH_ONLINE_MS = 75000;           // last_seen이 이 이내면 온라인(heartbeat 30s 여유)

  function relTime(iso) {
    const ms = Date.parse(iso);
    if (isNaN(ms)) return "";
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return "방금";
    if (s < 60) return `${s}초 전`;
    if (s < 3600) return `${Math.floor(s / 60)}분 전`;
    return `${Math.floor(s / 3600)}시간 전`;
  }
  function isOnline(d) {
    const age = Date.now() - Date.parse(d.last_seen || 0);
    return age >= 0 && age < HEALTH_ONLINE_MS;
  }
  function deviceLabel(d) { return d.name || d.machine_id || (d.id || "").slice(0, 8); }

  function renderHealth() {
    if (!healthChips) return;
    const list = Array.from(deviceMap.values()).filter((d) => !d.revoked)
      .sort((a, b) => (deviceLabel(a) < deviceLabel(b) ? -1 : 1));
    if (list.length === 0) {
      healthChips.innerHTML = '<span class="health-empty">등록된 머신 없음 — ⚙ 머신 관리에서 추가</span>';
      return;
    }
    healthChips.innerHTML = list.map((d) => {
      const on = isOnline(d);
      const warn = d.last_error
        ? `<span class="dev-warn" title="${esc(d.last_error)}">⚠</span>` : "";
      return `<span class="machine-chip ${on ? "online" : "stale"}" title="${esc(d.platform || "")} · v${esc(d.collector_version || "?")}">` +
        `<span class="mdot"></span><span class="mname">${esc(deviceLabel(d))}</span>${warn}` +
        `<span class="mseen">${on ? "온라인" : (d.last_seen ? relTime(d.last_seen) : "대기")}</span></span>`;
    }).join("");
  }
  async function loadDevices() {
    const { data, error } = await client.from("devices").select("*");
    if (!error) {
      deviceMap.clear();
      for (const d of data || []) deviceMap.set(d.id, d);
      renderHealth(); renderDeviceList();
    }
  }
  let devChannel = null, devResubTimer = null;
  function subscribeDevices() {
    if (devChannel) { try { client.removeChannel(devChannel); } catch (_) {} devChannel = null; }
    devChannel = client.channel("periscribe-devices")
      .on("postgres_changes", { event: "*", schema: "public", table: "devices" }, (p) => {
        if (p.new && p.new.id) {
          const prev = deviceMap.get(p.new.id);
          deviceMap.set(p.new.id, p.new);
          // 봉인 DEK(세대)가 새로 도착/변경되면 그 디바이스 캐시 무효화 + 잠긴 이벤트 재복호화.
          const changed = !prev
            || JSON.stringify(prev.dek_keys || {}) !== JSON.stringify(p.new.dek_keys || {})
            || prev.wrapped_dek !== p.new.wrapped_dek;
          const hasKey = p.new.wrapped_dek || (p.new.dek_keys && Object.keys(p.new.dek_keys).length);
          if (changed && hasKey) {
            for (const k of Array.from(dekCache.keys())) {
              if (k.indexOf(p.new.id + ":") === 0) dekCache.delete(k);
            }
            redecryptLocked();
          }
        } else if (p.old && p.old.id) deviceMap.delete(p.old.id);
        renderHealth(); renderDeviceList();
      }).subscribe((status) => {
        if (status === "SUBSCRIBED") loadDevices();   // (재)구독 시 디바이스 상태 재동기화
        else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT" || status === "CLOSED") {
          if (!devResubTimer) devResubTimer = setTimeout(() => { devResubTimer = null; subscribeDevices(); }, 3000);
        }
      });
  }
  setInterval(renderHealth, 5000);          // 상대시간/온라인 상태 주기 갱신
  // 절전/백그라운드/네트워크 복귀 시 누락분 따라잡기
  document.addEventListener("visibilitychange", () => { if (!document.hidden) catchUp(); });
  window.addEventListener("online", () => catchUp());

  // ---- 디바이스 관리(토큰 발급/revoke) ----
  function genToken() {
    const a = new Uint8Array(24); crypto.getRandomValues(a);
    return "pscb_" + Array.from(a).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  async function sha256hex(s) {
    const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  function renderDeviceList() {
    const el = document.getElementById("devices-list");
    if (!el) return;
    const list = Array.from(deviceMap.values()).sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
    if (list.length === 0) { el.innerHTML = '<div class="health-empty">아직 등록된 머신이 없습니다.</div>'; return; }
    el.innerHTML = list.map((d) => {
      const status = d.uninstalled_at ? '<span class="dev-revoked">🗑 제거됨</span>'
        : d.revoked ? '<span class="dev-revoked">revoked</span>'
        : isOnline(d) ? '<span class="dev-online">● 온라인</span>'
        : `<span class="dev-stale">● ${d.last_seen ? relTime(d.last_seen) : "대기"}</span>`;
      // 제거됨(uninstall): 삭제만 / 관리자 revoke: 재활성+삭제 / 활성: revoke
      const action = d.uninstalled_at
        ? `<button class="btn ghost btn-sm" data-delete="${d.id}">삭제</button>`
        : d.revoked
        ? `<button class="btn ghost btn-sm" data-restore="${d.id}">재활성</button> ` +
          `<button class="btn ghost btn-sm" data-delete="${d.id}">삭제</button>`
        : `<button class="btn ghost btn-sm" data-revoke="${d.id}">revoke</button>`;
      const warn = (d.last_error && !d.uninstalled_at)
        ? `<span class="dev-warn" title="${esc(d.last_error)}${d.last_error_at ? " (" + esc(relTime(d.last_error_at)) + ")" : ""}">⚠</span> `
        : "";
      return `<div class="device-row">
        <div class="dev-main"><b>${esc(deviceLabel(d))}</b>
          <span class="dev-meta">${esc(d.machine_id || "미연결")} · ${esc(d.platform || "")}</span></div>
        <div class="dev-status">${warn}${status}</div>${action}</div>`;
    }).join("");
  }
  async function addDevice(name) {
    const resultEl = document.getElementById("token-result");
    const token = genToken();
    const token_hash = await sha256hex(token);
    const { error } = await client.from("devices").insert({ owner_id: currentUserId, token_hash, name: name || null });
    if (error) {
      resultEl.style.display = "block";
      resultEl.innerHTML = '<span class="login-error">발급 실패: ' + esc(error.message) + "</span>";
      return;
    }
    resultEl.style.display = "block";
    resultEl.innerHTML =
      '<div class="token-warn">⚠ 이 토큰은 지금 한 번만 표시됩니다. 안전하게 보관하세요.</div>' +
      '<div class="token-label">디바이스 토큰</div><pre class="token-box">' + esc(token) + "</pre>" +
      '<div class="token-steps">설치: 위 <b>⬇ Collector 다운로드</b>로 받은 <b>periscribe.exe</b> 를 해당 PC에서 ' +
      '실행한 뒤, 이 토큰을 붙여넣고 Enter. (자동 설치 + 부팅 시 자동 실행)</div>';
    loadDevices();
  }

  const devModal = document.getElementById("devices-modal");
  const manageBtn = document.getElementById("manage-devices");
  if (manageBtn) manageBtn.addEventListener("click", () => {
    if (devModal) devModal.style.display = "flex";
    loadDevices();   // 열 때마다 DB에서 새로 동기화(유령 항목 방지)
  });
  const devClose = document.getElementById("devices-close");
  if (devClose) devClose.addEventListener("click", () => {
    if (devModal) devModal.style.display = "none";
    const tr = document.getElementById("token-result"); if (tr) tr.style.display = "none";
  });
  const addForm = document.getElementById("add-device-form");
  if (addForm) addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const inp = document.getElementById("device-name");
    await addDevice(inp.value.trim()); inp.value = "";
  });
  const devListEl = document.getElementById("devices-list");
  if (devListEl) devListEl.addEventListener("click", async (e) => {
    const t = e.target;
    if (!t || !t.getAttribute) return;
    const rev = t.getAttribute("data-revoke");
    if (rev) { await client.from("devices").update({ revoked: true }).eq("id", rev); loadDevices(); return; }
    const res = t.getAttribute("data-restore");
    if (res) {
      // 재활성: revoked 해제. 컬렉터가 아직 살아 백오프 중이면 자동 재개, 종료됐으면 재시작 필요.
      await client.from("devices").update({ revoked: false }).eq("id", res);
      loadDevices();
      return;
    }
    const del = t.getAttribute("data-delete");
    if (del) {
      if (!confirm("이 머신과 수집된 로그를 모두 영구 삭제합니다. 되돌릴 수 없습니다. 계속할까요?")) return;
      // 디바이스 행 + 그 머신의 events 까지 함께 삭제(소유 검증 내장 RPC).
      const { error } = await client.rpc("purge_device", { p_device: del });
      if (error) { alert("삭제 실패: " + error.message); return; }
      deviceMap.delete(del);
      loadDevices();
      loadHistory();   // 삭제된 머신의 로그를 피드에서도 즉시 반영
    }
  });

  // ---------- E2EE 게이트(키 셋업/잠금해제 모달) ----------
  function encShow(view) {
    ["enc-setup", "enc-recovery-show", "enc-unlock", "enc-recover"].forEach((id) => {
      const e = document.getElementById(id);
      if (e) e.style.display = (id === view) ? "" : "none";
    });
    const m = document.getElementById("enc-modal");
    if (m) m.style.display = "flex";
  }
  function encHide() { const m = document.getElementById("enc-modal"); if (m) m.style.display = "none"; }
  async function cachePriv() {
    try { sessionStorage.setItem("pscb_pk", bufToB64(await crypto.subtle.exportKey("pkcs8", ownerPrivKey))); }
    catch (e) { /* sessionStorage 불가 환경: 메모리만 사용(새로고침 시 재입력) */ }
  }
  function genRecoveryCode() {
    const a = new Uint8Array(20); crypto.getRandomValues(a);
    const hex = Array.from(a).map((b) => b.toString(16).padStart(2, "0")).join("").toUpperCase();
    return hex.match(/.{1,5}/g).join("-");
  }
  async function fetchOwnerKeys() {
    const { data, error } = await client.from("owner_keys").select("*").eq("owner_id", currentUserId).maybeSingle();
    return error ? null : (data || null);
  }
  async function ensureEncUnlocked() {
    ownerKeysRow = await fetchOwnerKeys();
    if (!ownerKeysRow) return encSetupFlow();
    const cached = sessionStorage.getItem("pscb_pk");
    if (cached) {
      try { ownerPrivKey = await importPriv(b64ToBuf(cached)); return true; }
      catch (e) { sessionStorage.removeItem("pscb_pk"); }
    }
    return encUnlockFlow();
  }

  function encSetupFlow() {
    return new Promise((resolve) => {
      encShow("enc-setup");
      const form = document.getElementById("enc-setup");
      const err = document.getElementById("enc-setup-err");
      form.onsubmit = async (e) => {
        e.preventDefault(); err.textContent = "";
        const p1 = document.getElementById("enc-pp1").value, p2 = document.getElementById("enc-pp2").value;
        if (p1.length < 8) { err.textContent = "패스프레이즈는 8자 이상이어야 합니다."; return; }
        if (p1 !== p2) { err.textContent = "패스프레이즈가 일치하지 않습니다."; return; }
        const btn = form.querySelector("button[type=submit]"); btn.disabled = true; btn.textContent = "생성 중…";
        try {
          const kp = await genOwnerKeypair();
          const salt = crypto.getRandomValues(new Uint8Array(16));
          const rsalt = crypto.getRandomValues(new Uint8Array(16));
          const kek = await deriveKEK(p1, salt, KDF_ITERS);
          const wrapped = await wrapPriv(kek, kp.privateKey);
          const recCode = genRecoveryCode();
          const rkek = await deriveKEK(recCode, rsalt, KDF_ITERS);
          const wrappedRec = await wrapPriv(rkek, kp.privateKey);
          const row = {
            owner_id: currentUserId,
            public_key: await exportPubSPKI(kp.publicKey),
            wrapped_private_key: wrapped,
            wrapped_private_key_recovery: wrappedRec,
            kdf: "pbkdf2-sha256",
            kdf_params: { salt: bufToB64(salt), iterations: KDF_ITERS, recovery_salt: bufToB64(rsalt) },
            kid: 1,
          };
          const { error } = await client.from("owner_keys").insert(row);
          if (error) { err.textContent = "저장 실패: " + error.message; btn.disabled = false; btn.textContent = "키 생성"; return; }
          ownerKeysRow = row; ownerPrivKey = kp.privateKey; await cachePriv();
          document.getElementById("enc-recovery-code").textContent = recCode;
          const ack = document.getElementById("enc-recovery-ack");
          const done = document.getElementById("enc-recovery-done");
          ack.checked = false; done.disabled = true;
          ack.onchange = () => { done.disabled = !ack.checked; };
          done.onclick = () => { encHide(); resolve(true); };
          encShow("enc-recovery-show");
        } catch (ex) {
          err.textContent = "오류: " + ex.message; btn.disabled = false; btn.textContent = "키 생성";
        }
      };
    });
  }

  function encUnlockFlow() {
    return new Promise((resolve) => {
      encShow("enc-unlock");
      const form = document.getElementById("enc-unlock");
      const err = document.getElementById("enc-unlock-err");
      form.onsubmit = async (e) => {
        e.preventDefault(); err.textContent = "";
        const pp = document.getElementById("enc-unlock-pp").value;
        const btn = form.querySelector("button[type=submit]"); btn.disabled = true; btn.textContent = "해제 중…";
        try {
          const kp = ownerKeysRow.kdf_params;
          const kek = await deriveKEK(pp, new Uint8Array(b64ToBuf(kp.salt)), kp.iterations || KDF_ITERS);
          ownerPrivKey = await unwrapPriv(kek, ownerKeysRow.wrapped_private_key);
          await cachePriv();
          document.getElementById("enc-unlock-pp").value = "";
          encHide(); resolve(true);
        } catch (ex) {
          err.textContent = "패스프레이즈가 올바르지 않습니다.";
          btn.disabled = false; btn.textContent = "잠금 해제";
        }
      };
      document.getElementById("enc-use-recovery").onclick = () => encRecoverFlow(resolve);
      document.getElementById("enc-recover-back").onclick = () => encShow("enc-unlock");
    });
  }

  function encRecoverFlow(resolve) {
    encShow("enc-recover");
    const form = document.getElementById("enc-recover");
    const err = document.getElementById("enc-recover-err");
    form.onsubmit = async (e) => {
      e.preventDefault(); err.textContent = "";
      const code = document.getElementById("enc-recover-code").value.trim().toUpperCase();
      const p1 = document.getElementById("enc-recover-pp1").value, p2 = document.getElementById("enc-recover-pp2").value;
      if (p1.length < 8) { err.textContent = "새 패스프레이즈는 8자 이상이어야 합니다."; return; }
      if (p1 !== p2) { err.textContent = "새 패스프레이즈가 일치하지 않습니다."; return; }
      if (!ownerKeysRow.wrapped_private_key_recovery) { err.textContent = "복구코드가 설정되어 있지 않습니다."; return; }
      const btn = form.querySelector("button[type=submit]"); btn.disabled = true; btn.textContent = "복구 중…";
      try {
        const kp = ownerKeysRow.kdf_params;
        const rkek = await deriveKEK(code, new Uint8Array(b64ToBuf(kp.recovery_salt)), kp.iterations || KDF_ITERS);
        ownerPrivKey = await unwrapPriv(rkek, ownerKeysRow.wrapped_private_key_recovery);
        const nsalt = crypto.getRandomValues(new Uint8Array(16));
        const nkek = await deriveKEK(p1, nsalt, KDF_ITERS);
        const nwrapped = await wrapPriv(nkek, ownerPrivKey);
        const nparams = Object.assign({}, kp, { salt: bufToB64(nsalt), iterations: KDF_ITERS });
        const { error } = await client.from("owner_keys")
          .update({ wrapped_private_key: nwrapped, kdf_params: nparams }).eq("owner_id", currentUserId);
        if (error) { err.textContent = "재설정 저장 실패: " + error.message; btn.disabled = false; btn.textContent = "복구 + 패스프레이즈 재설정"; return; }
        ownerKeysRow.wrapped_private_key = nwrapped; ownerKeysRow.kdf_params = nparams;
        await cachePriv();
        encHide(); resolve(true);
      } catch (ex) {
        err.textContent = "복구코드가 올바르지 않습니다.";
        btn.disabled = false; btn.textContent = "복구 + 패스프레이즈 재설정";
      }
    };
  }

  // 디바이스가 뒤늦게 wrapped_dek 를 받으면(부트스트랩 직후) 잠겨 있던 이벤트를 다시 복호화.
  async function redecryptLocked() {
    if (!ownerPrivKey) return;
    let changed = 0;
    for (const ev of store.values()) {
      if (ev.enc_version === 1 && !ev._dec) {
        ev._encLocked = false;
        await decryptEvent(ev);
        if (ev._dec) changed++;
      }
    }
    if (changed) render(false);
  }

  // ---------- 인증 게이트 ----------
  let appStarted = false;
  let inRecovery = false;  // 비번 재설정 링크로 들어온 동안엔 앱을 시작하지 않고 새 비번 폼을 보인다.
  async function initApp() {
    if (appStarted) return;
    appStarted = true;
    await loadDevices();                  // devices.wrapped_dek 확보(복호화에 필요)
    const ok = await ensureEncUnlocked(); // 키 셋업/잠금해제(취소 시 게이트 유지)
    if (!ok) { appStarted = false; return; }
    subscribeDevices();
    subscribeCatalog();                   // 새 세션이 카탈로그에 들어오면 목록 갱신
    loadFilterOptions();                  // DB 전체 세션/머신으로 드롭다운 채움
    await loadHistory();
    subscribe();
  }
  // 세션 카탈로그 변경(신규/수정) → 세션 드롭다운 갱신(디바운스).
  let catChannel = null, catDebounce = null;
  function subscribeCatalog() {
    if (catChannel) { try { client.removeChannel(catChannel); } catch (_) {} catChannel = null; }
    catChannel = client.channel("periscribe-catalog")
      .on("postgres_changes", { event: "*", schema: "public", table: "session_catalog" }, () => {
        clearTimeout(catDebounce);
        catDebounce = setTimeout(loadFilterOptions, 800);
      }).subscribe();
  }
  function showAuthed(session) {
    document.body.classList.add("authed");
    currentUserId = session && session.user ? session.user.id : null;
    const ub = document.getElementById("user-box");
    if (ub) ub.style.display = "";
    const ue = document.getElementById("user-email");
    if (ue && session && session.user) ue.textContent = session.user.email || "";
    initApp();
  }
  function showLogin() {
    document.body.classList.remove("authed");
    const ub = document.getElementById("user-box");
    if (ub) ub.style.display = "none";
  }
  // 새 비번 입력 폼으로 전환(로그인 폼 대신). body는 비인증 상태로 둬서 게이트가 보이게 한다.
  function showRecovery() {
    document.body.classList.remove("authed");
    const lf = document.getElementById("login-form");
    const rf = document.getElementById("recovery-form");
    if (lf) lf.style.display = "none";
    if (rf) rf.style.display = "";
  }

  const loginForm = document.getElementById("login-form");
  const loginError = document.getElementById("login-error");
  if (loginForm) loginForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    loginError.textContent = "";
    const email = document.getElementById("login-email").value.trim();
    const password = document.getElementById("login-password").value;
    const btn = document.getElementById("login-btn");
    btn.disabled = true; btn.textContent = "로그인 중…";
    const { error } = await client.auth.signInWithPassword({ email, password });
    btn.disabled = false; btn.textContent = "로그인";
    if (error) loginError.textContent = "로그인 실패: " + error.message;
  });
  const logoutBtn = document.getElementById("logout");
  if (logoutBtn) logoutBtn.addEventListener("click", async () => {
    try { sessionStorage.removeItem("pscb_pk"); } catch (e) { /* noop */ }
    ownerPrivKey = null; dekCache.clear();
    await client.auth.signOut();
    location.reload();
  });

  // 비밀번호 재설정 메일 발송. Supabase가 메일·토큰·검증을 처리하고, 링크는 redirectTo(이 앱)로 돌아온다.
  const forgotBtn = document.getElementById("forgot-btn");
  const loginInfo = document.getElementById("login-info");
  if (forgotBtn) forgotBtn.addEventListener("click", async () => {
    loginError.textContent = "";
    if (loginInfo) loginInfo.textContent = "";
    const email = document.getElementById("login-email").value.trim();
    if (!email) { loginError.textContent = "먼저 이메일을 입력하세요."; return; }
    forgotBtn.disabled = true;
    const { error } = await client.auth.resetPasswordForEmail(email, { redirectTo: location.origin });
    forgotBtn.disabled = false;
    if (error) loginError.textContent = "메일 발송 실패: " + error.message;
    else if (loginInfo) loginInfo.textContent = email + " 로 재설정 메일을 보냈습니다. 메일의 링크를 누르면 이 페이지로 돌아옵니다.";
  });

  // recovery 세션 상태에서 새 비번 확정. updateUser 가 앱이 채워야 할 마지막 한 조각.
  const recoveryForm = document.getElementById("recovery-form");
  const recoveryError = document.getElementById("recovery-error");
  if (recoveryForm) recoveryForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    recoveryError.textContent = "";
    const pw = document.getElementById("new-password").value;
    const btn = document.getElementById("recovery-btn");
    btn.disabled = true; btn.textContent = "변경 중…";
    const { error } = await client.auth.updateUser({ password: pw });
    btn.disabled = false; btn.textContent = "비밀번호 변경";
    if (error) { recoveryError.textContent = "변경 실패: " + error.message; return; }
    inRecovery = false;
    history.replaceState(null, "", location.pathname);  // URL 의 recovery 토큰 해시 제거
    location.reload();                                   // 정상 세션으로 앱 재시작
  });

  // ---------- 시작 ----------
  // 비번 재설정 링크로 들어오면 URL 해시에 type=recovery 가 실려 온다(SDK가 곧 해시를 정리하므로 동기적으로 먼저 확인).
  const _hp = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  if (_hp.get("type") === "recovery") { inRecovery = true; showRecovery(); }

  client.auth.getSession().then(({ data: { session } }) => {
    if (inRecovery) return;                 // recovery 중엔 앱 시작 보류(새 비번 폼만 노출)
    if (session) showAuthed(session); else showLogin();
  });
  client.auth.onAuthStateChange((event, session) => {
    if (event === "PASSWORD_RECOVERY") { inRecovery = true; showRecovery(); return; }
    if (inRecovery) return;
    if (session) showAuthed(session); else showLogin();
  });
})();
