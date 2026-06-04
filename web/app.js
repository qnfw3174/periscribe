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
    text: document.getElementById("f-text"),
    errors: document.getElementById("f-errors"),
  };

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
  function sortKey(ev) {
    // 머신 간 통합 순서는 received_at, 동일 세션 내는 ts. received_at 우선.
    return (ev.received_at || ev.ts || "") + "|" + (ev.event_id || "");
  }

  // ---------- 렌더 ----------
  function payloadText(ev) {
    const p = ev.payload || {};
    if (ev.kind === "tool_result") return p.output_full || "";
    if (ev.kind === "user_prompt" || ev.kind === "assistant_text") return p.text || "";
    if (ev.kind === "tool_use") {
      if (ev.tool === "Bash") return p.command || "";
      try { return JSON.stringify(p.input != null ? p.input : p, null, 2); }
      catch (e) { return String(p); }
    }
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
    if (ev.kind === "tool_use") return classifyAction(ev).severity;
    return "info";
  }
  function categoryOf(ev) {
    if (ev.kind === "tool_result") {
      const pair = pairedToolUse(ev);
      return pair ? classifyAction(pair).category : "";
    }
    if (ev.kind === "tool_use") return classifyAction(ev).category;
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
    const sidechain = ev.is_sidechain ? `<span class="sidechain">↳ sub</span>` : "";
    const sess = ev.session_id ? esc(ev.session_id).slice(0, 8) : "";

    el.innerHTML =
      `<div class="event__head">
         <span class="event__kind kind--${ev.kind}">${esc(ev.kind)}</span>
         ${toolBadge}${sevBadge}${catChip}
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

  function refreshFilterOptions() {
    const machines = new Set(), sessions = new Set(), cats = new Set();
    for (const ev of store.values()) {
      if (ev.machine_id) machines.add(ev.machine_id);
      if (ev.session_id) sessions.add(ev.session_id);
      const c = categoryOf(ev);
      if (c) cats.add(c);
    }
    syncSelect(F.machine, machines);
    syncSelect(F.session, sessions, 8);
    syncSelect(F.category, cats);
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

  function render() {
    const list = Array.from(store.values()).filter(passesFilter);
    list.sort((a, b) => (sortKey(a) < sortKey(b) ? -1 : 1));
    feedEl.innerHTML = "";
    if (list.length === 0) {
      feedEl.appendChild(emptyEl);
      emptyEl.textContent = store.size === 0 ? "이벤트 없음. Collector 가 도는지 확인하세요." : "필터에 맞는 이벤트 없음.";
      countEl.textContent = `${store.size}개 중 0개`;
      return;
    }
    const frag = document.createDocumentFragment();
    for (const ev of list) frag.appendChild(eventNode(ev));
    feedEl.appendChild(frag);
    countEl.textContent = `${store.size}개 중 ${list.length}개 표시`;
    window.scrollTo(0, document.body.scrollHeight);
  }

  // ---------- 데이터 ----------
  function upsert(ev) {
    if (!ev || !ev.event_id) return;
    store.set(ev.event_id, ev); // 멱등
  }

  async function loadHistory() {
    setConn("off", "과거 로드 중…");
    let q = client.from(table).select("*").order("received_at", { ascending: false }).limit(pageSize);
    if (F.machine.value) q = q.eq("machine_id", F.machine.value);
    if (F.session.value) q = q.eq("session_id", F.session.value);
    if (F.kind.value) q = q.eq("kind", F.kind.value);
    if (F.errors.checked) q = q.eq("is_error", true);
    const { data, error } = await q;
    if (error) {
      setConn("err", "조회 오류: " + error.message);
      emptyEl.textContent = "조회 오류: " + error.message + " (anon 키/RLS 확인)";
      return;
    }
    for (const ev of data || []) upsert(ev);
    refreshFilterOptions();
    render();
    setConn("on", "실시간 구독 중");
  }

  function subscribe() {
    client.channel("periscribe-events")
      .on("postgres_changes", { event: "INSERT", schema: "public", table: table }, (payload) => {
        upsert(payload.new);
        refreshFilterOptions();
        render();
      })
      .subscribe((status) => {
        if (status === "SUBSCRIBED") setConn("on", "실시간 구독 중");
        else if (status === "CHANNEL_ERROR" || status === "TIMED_OUT") setConn("err", "Realtime 오류");
      });
  }

  function setConn(state, text) {
    connDot.className = "dot dot--" + (state === "on" ? "on" : state === "err" ? "err" : "off");
    connText.textContent = text;
  }

  // ---------- 이벤트 바인딩 ----------
  let textDebounce;
  F.text.addEventListener("input", () => {
    clearTimeout(textDebounce);
    textDebounce = setTimeout(render, 150);
  });
  [F.machine, F.session, F.kind, F.severity, F.category, F.errors].forEach((el) =>
    el.addEventListener("change", render));
  document.getElementById("reload").addEventListener("click", loadHistory);

  // ---------- 시작 ----------
  loadHistory().then(subscribe);
})();
