"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const log = $("log"), input = $("t"), mic = $("mic"), sendBtn = $("send"), player = $("player");
  const OPEN = new Set(["SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING", "SCHEDULED"]);
  const PAID = new Set(["PAID", "MARKED_AS_PAID"]);
  const BAD = new Set(["CANCELLED", "REFUNDED", "PARTIALLY_REFUNDED", "MARKED_AS_REFUNDED"]);
  const SILENT_MP3 = "data:audio/mpeg;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4LjI5LjEwMAAAAAAAAAAAAAAA//tQxAADB8AhSmxhIIEVCSiJrDCQBTcu3UrAIwUdkRgQbFAZC1CQEwTJ9mjRvBA4UOLD8nKVOWfh+UlK3z/177OXrfOdKl7pyn3Xf//WreyTRUoAWgBgkOAGbZHBgG1OF6zM82DWbZaUmMBptgQhGjsyYqc9ae9XFz280948NMBWInljyzsNRFLPWdnZGWrddDsjK1unuSrVN9jJsK8KuQtQCtMBjCEtImISdNKJOopIpBFpNSMbIHCSRpRR5iakjTiyzLhchUUBwCgyKiweBv/7UsQbg8isVNoMPMjAAAA0gAAABEVFGmgqK////9bP/6XCykxBTUUzLjEwMKqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq";
  const store = {
    get(k, s) { try { return (s ? sessionStorage : localStorage).getItem(k); } catch (e) { return null; } },
    set(k, v, s) { try { (s ? sessionStorage : localStorage).setItem(k, v); } catch (e) { /* storage blocked */ } },
  };
  const uuid = () => {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    const b = new Uint8Array(16);
    if (window.crypto && crypto.getRandomValues) crypto.getRandomValues(b); else for (let i = 0; i < 16; i++) b[i] = Math.random() * 256 | 0;
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    const h = Array.from(b, x => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  };

  function initialLang() {
    const saved = store.get("dp_lang");
    if (saved && I18N[saved]) return saved;
    for (const l of navigator.languages || [navigator.language || "en"]) {
      const p = String(l).slice(0, 2).toLowerCase();
      if (I18N[p]) return p;
    }
    return "en";
  }

  let lang = initialLang();
  let ws = store.get("dp_ws");
  let conv = store.get("dp_conv", true) || uuid();
  let muted = store.get("dp_muted") === "1";
  let status = null, state = "idle", busy = false, lastAlert = 0, lastState = null, currency = "USD";
  const shownCards = new Set();
  const t = k => I18N[lang][k];
  const money = (v, cur) => `${Number(v || 0).toLocaleString(lang, { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${cur || currency}`;
  const fmtDate = iso => { if (!iso) return ""; const d = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso); return d.toLocaleDateString(lang, { weekday: "short", day: "numeric", month: "short" }); };
  const fmtShort = iso => { if (!iso) return ""; const d = new Date(iso + "T12:00:00"); return d.toLocaleDateString(lang, { day: "numeric", month: "short" }); };
  const fmtWhen = iso => { const d = new Date(iso); return d.toLocaleString(lang, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }); };

  const withTimeout = (opts, ms) => {
    const o = Object.assign({}, opts || {});
    if (window.AbortSignal && AbortSignal.timeout) o.signal = AbortSignal.timeout(ms);
    return o;
  };
  async function api(path, opts, ms) {
    const r = await fetch(path, withTimeout(opts, ms || 45000));
    if (!r.ok) { const e = new Error("http " + r.status); e.status = r.status; throw e; }
    return r.json();
  }
  const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

  // ---------- UI text ----------
  function applyTexts() {
    document.documentElement.lang = lang;
    $("lang").value = lang; $("lang-label").textContent = t("lang");
    $("tagline").textContent = t("tagline");
    input.placeholder = t("placeholder"); $("t-label").textContent = t("message"); sendBtn.textContent = t("send");
    $("footer").textContent = t("footer");
    $("chart-title").textContent = t("chart_title");
    $("legend").innerHTML = "";
    for (const [c, k] of [["var(--ok)", "chart_in"], ["var(--bad)", "chart_out"], ["var(--accent)", "chart_balance"]]) {
      const s = document.createElement("span"); const i = document.createElement("i"); i.style.background = c; s.append(i, t(k)); $("legend").appendChild(s);
    }
    $("status-hint").textContent = canListen() ? t("hint") : t("v_unsupported");
    for (const [id, key] of [["new-conv", "new_conv"], ["new-biz", "new_biz"]]) { $(id).title = t(key); $(id).setAttribute("aria-label", t(key)); }
    $("qr-title").textContent = t("qr_title"); $("qr-close").textContent = t("close");
    updateMute(); renderTabs(); renderChips(); applyStatus(); setState(state);
    if (lastState) render(lastState);
  }

  function renderChips() {
    const box = $("chips"); box.textContent = "";
    for (const ex of t("examples")) {
      const c = document.createElement("button"); c.type = "button"; c.className = "chip"; c.textContent = ex;
      c.addEventListener("click", () => { cancelListening(); send(ex); });
      box.appendChild(c);
    }
  }

  function applyStatus() {
    if (!status) return;
    $("mode").textContent = status.demo_mode ? t("mode_demo") : (status.paypal_env === "live" ? t("mode_live") : t("mode_sandbox"));
    $("voice-badge").hidden = !status.voice.tts; $("voice-badge").textContent = "🎧 " + t("voice_badge");
    const notice = !status.ai_ready ? t("ai_off") : status.demo_mode ? t("demo_note") : "";
    $("notice").hidden = !notice; $("notice").textContent = notice;
  }

  function setState(s) {
    state = s;
    mic.dataset.state = s;
    $("status").textContent = t("st_" + s);
    mic.setAttribute("aria-label", s === "listening" ? t("mic_stop") : t("mic_start"));
    mic.setAttribute("aria-pressed", s === "listening" ? "true" : "false");
    sendBtn.disabled = busy;
    if (s === "idle" && queue.length && !busy) setTimeout(flushQueue, 250);
  }

  function add(cls, text) {
    const d = document.createElement("div"); d.className = "msg " + cls; d.textContent = text;
    log.appendChild(d); log.scrollTop = log.scrollHeight; return d;
  }

  function addAction(a) {
    const d = document.createElement("div");
    const label = t("actions")[a.tool] || a.tool;
    const waiting = a.result && a.result.status === "awaiting_owner_confirmation";
    d.className = "act" + (a.error ? " err" : waiting ? " wait" : "");
    d.textContent = a.error ? `⚠ ${label}: ${a.error}` : `${waiting ? "⏳" : "✓"} ${label}`;
    log.appendChild(d); log.scrollTop = log.scrollHeight;
  }

  // ---------- voice output: Destiny's own voice, browser voice as fallback ----------
  let speakToken = 0, speakTimer = null, queue = [], unlocked = false, voices = [];
  const loadVoices = () => { try { voices = speechSynthesis.getVoices() || []; } catch (e) { voices = []; } };
  if ("speechSynthesis" in window) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }

  function unlockAudio() {
    if (unlocked) return; unlocked = true;
    try { player.src = SILENT_MP3; player.play().catch(() => {}); } catch (e) { /* ignore */ }
    try { if ("speechSynthesis" in window) { const u = new SpeechSynthesisUtterance(" "); u.volume = 0; speechSynthesis.speak(u); } } catch (e) { /* ignore */ }
  }

  function pickVoice() {
    const exact = SPEECH_LANG[lang].toLowerCase();
    const norm = v => String(v.lang || "").toLowerCase().replace("_", "-");
    return voices.find(v => norm(v) === exact) || voices.find(v => norm(v).startsWith(lang)) || null;
  }

  function browserSpeak(text, token, done) {
    if (!("speechSynthesis" in window)) { done(); return; }
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text); const v = pickVoice();
    if (v) u.voice = v; u.lang = v ? v.lang : SPEECH_LANG[lang];
    u.onend = done; u.onerror = done;
    setTimeout(() => { if (token === speakToken) speechSynthesis.speak(u); }, 60);
  }

  function speak(text, speechId) {
    cancelListening();  // never listen while speaking: the mic would hear Destiny's own voice
    if (muted || !text) { setState("idle"); return; }
    stopSpeaking();
    const token = ++speakToken;
    const done = () => { if (token !== speakToken) return; clearTimeout(speakTimer); if (state === "speaking") setState("idle"); };
    let fellBack = false;
    const fallback = () => { if (token !== speakToken || fellBack) return; fellBack = true; browserSpeak(text, token, done); };
    setState("speaking");
    clearTimeout(speakTimer);
    speakTimer = setTimeout(done, 6000 + text.length * 110);  // never stay stuck in "speaking"
    if (speechId && status && status.voice.tts) {
      player.onended = done;
      player.onerror = fallback;
      player.src = "/api/speech/" + encodeURIComponent(speechId);
      player.play().catch(fallback);
    } else browserSpeak(text, token, done);
  }

  function stopSpeaking() {
    speakToken++;
    try { player.pause(); player.onended = player.onerror = null; } catch (e) { /* ignore */ }
    if ("speechSynthesis" in window) speechSynthesis.cancel();
    clearTimeout(speakTimer);
    if (state === "speaking") setState("idle");
  }

  function sayWhenIdle(text, speechId) { if (state === "idle" && !busy) speak(text, speechId); else queue.push([text, speechId]); }
  function flushQueue() { if (state === "idle" && !busy && queue.length) { const [x, y] = queue.shift(); speak(x, y); } }

  function updateMute() {
    const b = $("mute"); b.textContent = muted ? "🔇" : "🔊";
    b.setAttribute("aria-pressed", muted ? "true" : "false"); b.setAttribute("aria-label", muted ? t("unmute") : t("mute")); b.title = b.getAttribute("aria-label");
  }

  // ---------- voice input: browser dictation, Destiny transcription as fallback ----------
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const canRecord = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  let preferRecording = false, rec = null, finalText = "", alternatives = [], recError = null, recCancelled = false;
  let recorder = null, chunks = [], recStream = null, silenceTimer = null, audioCtx = null;
  const serverSTT = () => !!(status && status.voice.stt && canRecord);
  const canListen = () => !!SR || serverSTT();

  function startListening() {
    if (busy) return;
    unlockAudio(); queue = []; stopSpeaking();
    if (SR && !preferRecording) startDictation(); else if (serverSTT()) startRecording(); else add("bot", t("v_unsupported"));
  }

  function startDictation() {
    finalText = ""; alternatives = []; recError = null; recCancelled = false; input.value = "";
    rec = new SR(); rec.lang = SPEECH_LANG[lang]; rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 3;
    rec.onresult = e => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        if (r.isFinal) { finalText += r[0].transcript; alternatives = Array.from(r).slice(1).map(a => a.transcript); }
        else interim += r[0].transcript;
      }
      input.value = (finalText + " " + interim).trim();
    };
    rec.onerror = e => { recError = e.error; };
    rec.onend = () => {
      rec = null;
      if (recCancelled) { recCancelled = false; finalText = ""; if (state === "listening") { input.value = ""; setState("idle"); } return; }
      const text = (finalText || input.value).trim();
      if (text) { send(text, alternatives); return; }
      setState("idle");
      if (recError && ["network", "service-not-allowed", "language-not-supported"].includes(recError) && serverSTT()) {
        preferRecording = true; add("bot", t("v_network")); recError = null; return;
      }
      const key = { "not-allowed": "v_denied", "service-not-allowed": "v_denied", "no-speech": "v_nospeech", "audio-capture": "v_nomic", "network": "v_generic" }[recError];
      if (recError && recError !== "aborted") add("bot", t(key || "v_generic"));
      recError = null;
    };
    try { rec.start(); setState("listening"); } catch (e) { rec = null; setState("idle"); add("bot", t("v_generic")); }
  }

  let starting = false;
  async function startRecording() {
    if (starting || recorder) return;  // a second tap while the permission prompt is open
    starting = true;
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch (e) { starting = false; add("bot", t(e && e.name === "NotFoundError" ? "v_nomic" : "v_denied")); return; }
    starting = false;
    const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"].find(x => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(x)) || "";
    chunks = []; recCancelled = false;
    let r;
    try { r = new MediaRecorder(stream, type ? { mimeType: type } : undefined); }
    catch (e) { stream.getTracks().forEach(tr => tr.stop()); add("bot", t("v_generic")); return; }
    recStream = stream; recorder = r;
    r.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    r.onstop = onRecordingStop;
    r.start();
    setState("listening");
    watchSilence(stream);
    setTimeout(() => { if (r.state === "recording") r.stop(); }, 20000);  // this recorder only, never a newer one
  }

  function watchSilence(stream) {
    try {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const src = audioCtx.createMediaStreamSource(stream); const an = audioCtx.createAnalyser(); an.fftSize = 1024; src.connect(an);
      const buf = new Uint8Array(an.fftSize); let heard = false, quietSince = 0;
      silenceTimer = setInterval(() => {
        an.getByteTimeDomainData(buf);
        let sum = 0; for (const v of buf) sum += (v - 128) * (v - 128);
        const rms = Math.sqrt(sum / buf.length);
        if (rms > 8) { heard = true; quietSince = 0; } else if (heard) { quietSince = quietSince || Date.now(); if (Date.now() - quietSince > 1300) stopListening(); }
      }, 100);
    } catch (e) { /* no analyser: the user taps to stop */ }
  }

  async function onRecordingStop() {
    clearInterval(silenceTimer); try { audioCtx && audioCtx.close(); } catch (e) { /* ignore */ }
    if (recStream) recStream.getTracks().forEach(tr => tr.stop());
    const blob = new Blob(chunks, { type: (recorder && recorder.mimeType) || "audio/webm" }); recorder = null;
    const cancelled = recCancelled; recCancelled = false;
    if (cancelled || blob.size < 1500) { if (state === "listening") setState("idle"); if (!cancelled) add("bot", t("v_nospeech")); return; }
    setState("transcribing");
    const fd = new FormData();
    fd.append("workspace_id", ws); fd.append("lang", lang);
    fd.append("audio", blob, blob.type.includes("mp4") ? "speech.m4a" : blob.type.includes("ogg") ? "speech.ogg" : "speech.webm");
    try {
      const r = await fetch("/api/transcribe", withTimeout({ method: "POST", body: fd }, 45000));
      const data = r.ok ? await r.json() : null;
      setState("idle");
      if (data && data.text) send(data.text); else add("bot", t("v_nospeech"));
    } catch (e) { setState("idle"); add("bot", t("offline")); }
  }

  function stopListening() {
    if (rec) { try { rec.stop(); } catch (e) { /* stopped */ } }
    if (recorder && recorder.state === "recording") recorder.stop();
  }
  function cancelListening() {
    if (rec) { recCancelled = true; try { rec.abort(); } catch (e) { rec = null; recCancelled = false; setState("idle"); } }
    if (recorder && recorder.state === "recording") { recCancelled = true; recorder.stop(); }
  }

  mic.addEventListener("click", () => { unlockAudio(); if (state === "listening") stopListening(); else if (state !== "thinking" && state !== "transcribing") startListening(); });

  // ---------- conversation ----------
  async function send(text, alts) {
    text = String(text || "").trim();
    if (!text || busy) return;
    unlockAudio(); queue = []; stopSpeaking();
    busy = true; input.value = "";
    add("me", text);
    const wait = add("bot pending", t("st_thinking"));
    wait.setAttribute("aria-hidden", "true");  // the status line already announces it
    setState("thinking");
    let data;
    try {
      data = await post("/api/chat", { workspace_id: ws, conversation_id: conv, message: text, lang, alternatives: (alts || []).slice(0, 3) });
    } catch (e) {
      data = { reply: t(e.status === 429 ? "rate" : e.status ? "server" : "offline"), error: "http" };
    }
    wait.remove();
    epoch++;
    for (const a of data.actions || []) addAction(a);
    for (const u of data.ui || []) { if (u.qr_png_base64) showQR(u); if (u.confirmation) addCard(u.confirmation); }
    const reply = data.reply || t("server");
    add("bot", reply);
    if (data.confirmed) dropCard(data.confirmed);
    if (data.error === "http" && !(data.actions || []).length) input.value = text;
    if (data.error === "conversation_full") newConversation(true);
    busy = false;
    speak(reply, data.speech_id);
    refresh();
  }

  $("f").addEventListener("submit", e => { e.preventDefault(); const text = input.value; cancelListening(); send(text); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") { cancelListening(); queue = []; stopSpeaking(); } });

  // ---------- confirmation cards (the only way money moves) ----------
  function cardFromState(c) { return { confirmation_id: c.id, action: c.kind, details: c.details }; }

  function addCard(c) {
    if (shownCards.has(c.confirmation_id)) return;
    shownCards.add(c.confirmation_id);
    const d = c.details || {};
    const box = document.createElement("div"); box.className = "conf"; box.dataset.id = c.confirmation_id;
    const h = document.createElement("h3"); h.textContent = "🔐 " + t("conf_" + c.action);
    const who = document.createElement("div"); who.className = "who"; who.textContent = `${t("conf_to")}: ${d.customer || d.payee || ""}`;
    const mail = document.createElement("div"); mail.className = "mail"; mail.textContent = d.email || "";
    box.append(h, who, mail);
    if ((d.items || []).length) {
      const ul = document.createElement("ul");
      for (const it of d.items) { const li = document.createElement("li"); li.textContent = `${it.quantity} × ${it.name} · ${money(it.unit_price, d.currency)}`; ul.appendChild(li); }
      box.appendChild(ul);
    } else if (d.description) { const p = document.createElement("div"); p.className = "mail"; p.textContent = d.description; box.appendChild(p); }
    const total = document.createElement("div"); total.className = "total"; total.textContent = `${t("conf_total")}: ${money(d.total != null ? d.total : d.amount, d.currency)}`;
    const row = document.createElement("div"); row.className = "row";
    const yes = document.createElement("button"); yes.className = "yes"; yes.type = "button"; yes.textContent = t("confirm");
    const no = document.createElement("button"); no.className = "no"; no.type = "button"; no.textContent = t("cancel");
    yes.addEventListener("click", () => resolveCard(c.confirmation_id, true, box));
    no.addEventListener("click", () => resolveCard(c.confirmation_id, false, box));
    row.append(yes, no);
    const hint = document.createElement("div"); hint.className = "hint"; hint.textContent = t("conf_hint");
    box.append(total, row, hint);
    $("cards").appendChild(box);
  }

  function dropCard(id) { const el = $("cards").querySelector(`[data-id="${id}"]`); if (el) el.remove(); shownCards.delete(id); }

  async function resolveCard(id, approve, box) {
    unlockAudio(); cancelListening(); stopSpeaking();
    box.querySelectorAll("button").forEach(b => b.disabled = true);
    try {
      const r = await post("/api/confirm", { workspace_id: ws, conversation_id: conv, confirmation_id: id, approve, lang });
      epoch++;
      dropCard(id);
      add(r.done ? "bot alert" : "bot", r.reply);
      speak(r.reply, r.speech_id);
    } catch (e) {
      box.querySelectorAll("button").forEach(b => b.disabled = false);
      add("bot", t(e.status === 429 ? "rate" : "server"));
    }
    refresh();
  }

  // ---------- dashboard ----------
  const TABS = ["invoices", "bills", "reminders", "contacts", "activity", "memory"];
  let tab = store.get("dp_tab") || "invoices";
  function renderTabs() {
    const box = $("tabs"); box.textContent = "";
    for (const name of TABS) {
      const b = document.createElement("button"); b.type = "button"; b.className = "tab"; b.role = "tab";
      b.textContent = t("tab_" + name); b.setAttribute("aria-selected", name === tab ? "true" : "false");
      b.id = "tab-" + name; b.setAttribute("aria-controls", "panel-" + name);
      b.addEventListener("click", () => { tab = name; tabChosen = true; store.set("dp_tab", tab); renderTabs(); });
      box.appendChild(b);
    }
    document.querySelectorAll(".panel").forEach(p => { p.hidden = p.dataset.panel !== tab; p.id = "panel-" + p.dataset.panel; p.setAttribute("role", "tabpanel"); p.setAttribute("aria-labelledby", "tab-" + p.dataset.panel); });
  }

  function kpi(label, value, foot, cls, footCls) {
    const d = document.createElement("div"); d.className = "kpi";
    const l = document.createElement("div"); l.className = "label"; l.textContent = label;
    const v = document.createElement("div"); v.className = "value " + (cls || "");
    const [num, cur] = String(value).split(" "); v.textContent = num;
    if (cur) { const sm = document.createElement("small"); sm.textContent = cur; v.appendChild(sm); }
    const f = document.createElement("div"); f.className = "foot " + (footCls || ""); f.textContent = foot || " ";
    d.append(l, v, f); return d;
  }

  function renderKpis(o) {
    const box = $("kpis"); box.textContent = "";
    const next = (o.next_bills || [])[0];
    box.append(
      kpi(t("kpi_receivable"), money(o.receivable), o.overdue_invoices.length ? t("overdue_n")(o.overdue_invoices.length) : t("open_n")(o.open_invoices), "", o.overdue_invoices.length ? "bad" : ""),
      kpi(t("kpi_bills"), money(o.bills_next_7_days), next ? t("next_bill")(next.payee, fmtDate(next.due_date)) : "", "", o.bills_overdue.length ? "bad" : ""),
      kpi(t("kpi_income"), money(o.income), t("net")(money(o.net)), "pos"),
      kpi(t("kpi_expense"), money(o.expense), o.top_expense_categories[0] ? `${t("cat")[o.top_expense_categories[0].category] || o.top_expense_categories[0].category} · ${money(o.top_expense_categories[0].amount)}` : ""),
      kpi(t("kpi_proj"), money(o.projected_30_days), "", o.projected_30_days < 0 ? "neg" : "pos"),
    );
  }

  function renderChart(days) {
    const svg = $("chart"); const W = 600, H = 170, pad = 6, mid = H * 0.55;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const max = Math.max(1, ...days.map(d => Math.max(d.in, d.out)));
    let run = 0; const pts = [];
    const cum = days.map(d => (run += d.in - d.out));
    const cmax = Math.max(1, ...cum.map(Math.abs));
    const bw = (W - pad * 2) / days.length;
    let out = `<line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="var(--line)"/>`;
    days.forEach((d, i) => {
      const x = pad + i * bw + bw * 0.18, w = bw * 0.64;
      if (d.in) { const h = (d.in / max) * (mid - 12); out += `<rect x="${x}" y="${mid - h}" width="${w}" height="${h}" rx="2" fill="var(--ok)"><title>${d.date}: +${money(d.in)}</title></rect>`; }
      if (d.out) { const h = (d.out / max) * (H - mid - 12); out += `<rect x="${x}" y="${mid}" width="${w}" height="${h}" rx="2" fill="var(--bad)"><title>${d.date}: -${money(d.out)}</title></rect>`; }
      const y = cum[i] >= 0 ? mid - (cum[i] / cmax) * (mid - 10) : mid + (-cum[i] / cmax) * (H - mid - 8);
      pts.push(`${pad + i * bw + bw / 2},${y}`);
    });
    out += `<polyline points="${pts.join(" ")}" fill="none" stroke="var(--accent)" stroke-width="2"/>`;
    svg.innerHTML = out;
    svg.setAttribute("aria-label", t("chart_title"));
  }

  // AG Grid tables
  const grids = {};
  const gridTheme = () => {
    const dark = window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches;
    return agGrid.themeQuartz.withParams(dark
      ? { backgroundColor: "#0d1b3e", foregroundColor: "#eef3ff", headerBackgroundColor: "#122450", headerTextColor: "#cfe0ff", borderColor: "#22355f", accentColor: "#4da3ff", fontSize: 13, headerFontWeight: 700 }
      : { backgroundColor: "#ffffff", foregroundColor: "#0b1a3c", headerBackgroundColor: "#f2f6fc", headerTextColor: "#003087", borderColor: "#dde3ee", accentColor: "#0070e0", fontSize: 13, headerFontWeight: 700 });
  };
  function pill(text, cls) { const s = document.createElement("span"); s.className = "pill " + (cls || ""); s.textContent = text; return s; }
  function statusPill(p) {
    const r = p.data; if (!r) return "";
    const key = r.overdue ? "overdue" : r.status;
    const cls = r.overdue ? "bad" : PAID.has(r.status) || r.status === "paid" ? "ok" : OPEN.has(r.status) || r.status === "pending" || r.status === "processing" ? "warn" : BAD.has(r.status) ? "bad" : "";
    return pill(t("status")[key] || r.status, cls);
  }
  function invoiceActions(p) {
    const r = p.data; const box = document.createElement("span"); if (!r) return box;
    if (r.payer_link) { const a = document.createElement("a"); a.className = "cell-btn"; a.href = r.payer_link; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = t("pay_link"); box.appendChild(a); }
    if (status && status.demo_mode && OPEN.has(r.status)) {
      const b = document.createElement("button"); b.className = "cell-btn"; b.type = "button"; b.textContent = t("simulate");
      b.addEventListener("click", async () => { b.disabled = true; try { await api(`/api/demo/pay/${encodeURIComponent(r.id)}?ws=${ws}`, { method: "POST" }); } catch (e) { /* refresh shows it */ } refresh(); });
      box.appendChild(b);
    }
    return box;
  }
  function billActions(p) {
    const r = p.data; const box = document.createElement("span"); if (!r || r.status !== "pending") return box;
    const b = document.createElement("button"); b.className = "cell-btn"; b.type = "button"; b.textContent = t("pay_paypal");
    b.addEventListener("click", () => send(t("cmd_pay_bill")(r.payee, r.id)));
    box.appendChild(b); return box;
  }
  function twoLines(p) {  // payee in bold, what it is for underneath: nothing gets cut off
    const r = p.data; const box = document.createElement("div"); box.className = "two-lines"; if (!r) return box;
    const a = document.createElement("strong"); a.textContent = r.payee;
    const b = document.createElement("span"); b.textContent = [r.description, t("recurrence")[r.recurrence]].filter(Boolean).join(" · ");
    box.append(a, b); return box;
  }
  function columns(name) {
    const m = p => p.value == null ? "" : money(p.value, p.data && p.data.currency);
    const d = p => fmtShort(p.value);
    if (name === "invoices") return [
      { field: "customer", headerName: t("col_customer"), flex: 1.2, minWidth: 110 },
      { field: "total", headerName: t("col_total"), valueFormatter: m, minWidth: 115, flex: 0.9 },
      { field: "due_date", headerName: t("col_due_date"), valueFormatter: d, minWidth: 95, flex: 0.8 },
      { field: "status", headerName: t("col_status"), cellRenderer: statusPill, minWidth: 125, flex: 1 },
      { headerName: t("col_actions"), cellRenderer: invoiceActions, width: 230, sortable: false },
    ];
    if (name === "bills") return [
      { field: "payee", headerName: t("col_payee"), flex: 2, minWidth: 170, cellRenderer: twoLines },
      { field: "amount", headerName: t("col_amount"), valueFormatter: m, minWidth: 115, flex: 0.9 },
      { field: "due_date", headerName: t("col_due_date"), valueFormatter: d, minWidth: 118, flex: 0.8, sort: "asc" },
      { field: "status", headerName: t("col_status"), cellRenderer: statusPill, minWidth: 120, flex: 0.9 },
      { headerName: t("col_actions"), cellRenderer: billActions, width: 155, sortable: false },
    ];
    return [
      { field: "name", headerName: t("col_name"), flex: 1.2, minWidth: 140 },
      { field: "email", headerName: t("col_email"), flex: 1.6, minWidth: 180 },
      { field: "kind", headerName: t("col_kind"), valueFormatter: p => t("kind")[p.value] || p.value, minWidth: 110, flex: 0.8 },
      { field: "notes", headerName: t("col_notes"), flex: 2, minWidth: 200 },
    ];
  }
  function setGrid(name, rows) {
    const el = $("grid-" + name);
    if (grids[name] && grids[name].__lang !== lang) { grids[name].destroy(); delete grids[name]; }  // rebuild so headers and texts follow the language
    if (!grids[name]) {
      grids[name] = agGrid.createGrid(el, { theme: gridTheme(), columnDefs: columns(name), rowData: rows, getRowId: p => String(p.data.id || p.data.name), rowHeight: name === "bills" ? 54 : 44,
        defaultColDef: { sortable: true, filter: false, resizable: true }, animateRows: true,
        localeText: { noRowsToShow: t("no_rows"), loadingOoo: "…" } });
      grids[name].__lang = lang; grids[name].__json = JSON.stringify(rows);
    } else {
      const json = JSON.stringify(rows);
      if (grids[name].__json !== json) { grids[name].setGridOption("rowData", rows); grids[name].__json = json; }  // no redraw when nothing changed
    }
  }
  if (window.matchMedia) matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { for (const g of Object.values(grids)) g.setGridOption("theme", gridTheme()); });

  function renderList(id, items, empty, fmt) {
    const ul = $(id); ul.textContent = "";
    if (!items.length) { const li = document.createElement("li"); li.className = "empty"; li.textContent = empty; ul.appendChild(li); return; }
    for (const it of items) ul.appendChild(fmt(it));
  }
  function li(when, text) { const el = document.createElement("li"); if (when) { const w = document.createElement("span"); w.className = "when"; w.textContent = when; el.appendChild(w); } el.append(text); return el; }

  let tabChosen = !!store.get("dp_tab");
  function render(s) {
    if (!lastState && !tabChosen && !s.invoices.length && s.bills.length) { tab = "bills"; renderTabs(); }
    lastState = s; currency = s.business.currency;
    $("biz-name").textContent = s.business.name;
    $("sample-badge").hidden = !s.business.sample; $("sample-badge").textContent = t("sample");
    renderKpis(s.overview); renderChart(s.cashflow);
    if (window.agGrid) { setGrid("invoices", s.invoices); setGrid("bills", s.bills); setGrid("contacts", s.contacts); }
    renderList("reminders", s.reminders, t("empty_reminders"), r => li(fmtWhen(r.due_at), (r.kind === "remind_customer" ? "📧 " : "⏰ ") + r.text));
    renderList("activity", s.activity, t("empty_activity"), a => li(new Date(a.at * 1000).toLocaleString(lang, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }), a.text));
    renderList("memory", s.facts, t("empty_memory"), f => li("", "🧠 " + f));
    for (const c of s.confirmations) addCard(cardFromState(c));
    const live = new Set(s.confirmations.map(c => c.id));
    $("cards").querySelectorAll(".conf").forEach(el => { if (!live.has(el.dataset.id)) { shownCards.delete(el.dataset.id); el.remove(); } });
  }

  let refreshing = false, firstLoad = true, epoch = 0, again = false;
  async function refresh() {
    if (!ws) { try { await createWorkspace(); } catch (e) { /* shown at start */ } return; }
    if (refreshing) { again = true; return; }  // run once more after the current poll instead of dropping it
    refreshing = true;
    const myEpoch = epoch;
    try {
      const s = await api(`/api/state?ws=${ws}&lang=${lang}&since=${lastAlert}`, undefined, 15000);
      if (myEpoch !== epoch) { again = true; return; }  // a chat or confirmation happened meanwhile: this snapshot is stale
      render(s);
      if (!firstLoad) for (const a of s.alerts) { add("bot alert", a.text); sayWhenIdle(a.text, a.speech_id); }
      firstLoad = false;  // alerts from before this visit are not replayed
      lastAlert = Math.max(lastAlert, s.last_alert_id || 0);
    } catch (e) {
      if (e.status === 404) { refreshing = false; await createWorkspace(); }
    } finally {
      refreshing = false;
      if (again) { again = false; setTimeout(refresh, 50); }
    }
  }

  function showQR(u) {
    const body = $("qr-body"); body.textContent = "";
    const img = new Image(); img.alt = t("qr_title");
    img.onload = () => { if (img.naturalWidth < 20) { body.textContent = ""; const p = document.createElement("p"); p.textContent = t("qr_demo"); body.appendChild(p); } };
    img.src = "data:image/png;base64," + u.qr_png_base64; body.appendChild(img);
    const linkBox = $("qr-link"); linkBox.textContent = "";
    const row = lastState && lastState.invoices.find(i => i.id === u.invoice_id);
    if (row && row.payer_link) { const a = document.createElement("a"); a.className = "cell-btn"; a.href = row.payer_link; a.target = "_blank"; a.rel = "noopener noreferrer"; a.textContent = t("pay_link"); linkBox.appendChild(a); }
    try { $("qr").showModal(); } catch (e) { $("qr").setAttribute("open", ""); }
  }

  // ---------- sessions ----------
  async function createWorkspace() {
    const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "America/Lima";
    const r = await post("/api/workspace", { lang, tz, sample: true });
    ws = r.workspace_id; store.set("dp_ws", ws); lastAlert = 0; firstLoad = true; shownCards.clear(); $("cards").textContent = "";
    await refresh();
  }
  function newConversation(keepLog) {
    conv = uuid(); store.set("dp_conv", conv, true);
    if (!keepLog) { log.textContent = ""; add("bot", t("greet")); }
  }
  async function briefing() {
    try {
      const b = await api(`/api/briefing?ws=${ws}&lang=${lang}`);
      const d = add("bot brief", b.text);
      const play = document.createElement("button"); play.className = "play"; play.type = "button"; play.textContent = "🔊 " + t("play_brief");
      play.addEventListener("click", () => { unlockAudio(); speak(b.text, b.speech_id); });
      d.appendChild(play);
    } catch (e) { /* briefing is optional */ }
  }

  $("lang").addEventListener("change", async e => {
    if (busy) { e.target.value = lang; return; }  // wait for the reply in flight
    cancelListening(); queue = []; stopSpeaking();
    lang = I18N[e.target.value] ? e.target.value : "en"; store.set("dp_lang", lang);
    newConversation(false);  // the assistant's language is fixed per conversation; memory stays
    applyTexts(); await refresh(); briefing();
  });
  $("mute").addEventListener("click", () => { muted = !muted; store.set("dp_muted", muted ? "1" : "0"); if (muted) { queue = []; stopSpeaking(); } updateMute(); });
  $("new-conv").addEventListener("click", () => { if (busy) return; cancelListening(); queue = []; stopSpeaking(); newConversation(false); briefing(); });
  $("new-biz").addEventListener("click", async () => {
    if (busy || !confirm(t("new_biz_q"))) return;
    cancelListening(); queue = []; stopSpeaking(); newConversation(false); await createWorkspace(); briefing();
  });

  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 5000);

  // ---------- start ----------
  (async () => {
    store.set("dp_conv", conv, true);
    applyTexts();
    add("bot", t("greet"));
    try { status = await api("/api/status"); } catch (e) { status = { demo_mode: true, voice: { tts: false, stt: false }, ai_ready: false }; }
    if (!SR && !serverSTT()) mic.disabled = true;
    applyTexts();
    try {
      if (!ws) await createWorkspace(); else await refresh();
    } catch (e) {
      add("bot", t(e.status === 429 ? "rate" : "offline"));
      const retry = setInterval(async () => { try { await createWorkspace(); clearInterval(retry); briefing(); } catch (err) { /* keep trying */ } }, 20000);
      return;
    }
    if (!ws) return;
    briefing();
  })();
})();
