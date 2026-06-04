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

  function eventNode(ev) {
    const el = document.createElement("article");
    const isBash = ev.kind === "tool_use" && ev.tool === "Bash";
    const isErr = ev.kind === "tool_result" && ev.is_error === true;
    el.className = "event event--" + ev.kind +
      (isBash ? " is-bash" : "") + (isErr ? " is-error" : "");
    el.dataset.eventId = ev.event_id;
    el.dataset.full = payloadText(ev);

    const toolBadge = ev.tool
      ? `<span class="tool-badge ${isBash ? "bash" : ""}">${esc(ev.tool)}</span>` : "";
    const sidechain = ev.is_sidechain ? `<span class="sidechain">↳ sub</span>` : "";
    const sess = ev.session_id ? esc(ev.session_id).slice(0, 8) : "";

    el.innerHTML =
      `<div class="event__head">
         <span class="event__kind kind--${ev.kind}">${esc(ev.kind)}</span>
         ${toolBadge}
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
    const q = F.text.value.trim().toLowerCase();
    if (q) {
      const hay = (payloadText(ev) + " " + (ev.tool || "")).toLowerCase();
      if (hay.indexOf(q) === -1) return false;
    }
    return true;
  }

  function refreshFilterOptions() {
    const machines = new Set(), sessions = new Set();
    for (const ev of store.values()) {
      if (ev.machine_id) machines.add(ev.machine_id);
      if (ev.session_id) sessions.add(ev.session_id);
    }
    syncSelect(F.machine, machines);
    syncSelect(F.session, sessions, 8);
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
  [F.machine, F.session, F.kind, F.errors].forEach((el) =>
    el.addEventListener("change", render));
  document.getElementById("reload").addEventListener("click", loadHistory);

  // ---------- 시작 ----------
  loadHistory().then(subscribe);
})();
