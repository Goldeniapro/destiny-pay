"use strict";
// Destiny Pay v5 interface. All money actions go through the server's authorization gate:
// the assistant only prepares proposals, the owner authorizes one exact operation, the server executes it.
(() => {
  const $ = id => document.getElementById(id);
  const $$ = sel => Array.from(document.querySelectorAll(sel));
  const log = $("log"), input = $("t"), mic = $("mic"), sendBtn = $("send"), player = $("player"), studio = $("studio");
  const OPEN = new Set(["SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING", "SCHEDULED"]);
  const PAID = new Set(["PAID", "MARKED_AS_PAID"]);
  const BAD = new Set(["CANCELLED", "REFUNDED", "PARTIALLY_REFUNDED", "MARKED_AS_REFUNDED"]);
  const SILENT_MP3 = "data:audio/mpeg;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4LjI5LjEwMAAAAAAAAAAAAAAA//tQxAADB8AhSmxhIIEVCSiJrDCQBTcu3UrAIwUdkRgQbFAZC1CQEwTJ9mjRvBA4UOLD8nKVOWfh+UlK3z/177OXrfOdKl7pyn3Xf//WreyTRUoAWgBgkOAGbZHBgG1OF6zM82DWbZaUmMBptgQhGjsyYqc9ae9XFz280948NMBWInljyzsNRFLPWdnZGWrddDsjK1unuSrVN9jJsK8KuQtQCtMBjCEtImISdNKJOopIpBFpNSMbIHCSRpRR5iakjTiyzLhchUUBwCgyKiweBv/7UsQbg8isVNoMPMjAAAAA0gAAABEVFGmgqK////9bP/6XCykxBTUUzLjEwMKqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq";
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
  let status = null, state = "idle", busy = false, lastAlert = 0, lastState = null, currency = "USD", briefingData = null;
  const t = k => I18N[lang][k];
  const u = (k, p) => {
    let s = (UI[lang] && UI[lang][k] !== undefined) ? UI[lang][k] : (UI.es[k] !== undefined ? UI.es[k] : k);
    if (p && typeof s === "string") s = s.replace(/\{(\w+)\}/g, (_, x) => (p[x] !== undefined ? p[x] : ""));
    return s;
  };
  function moneyParts(v, cur) {
    const parts = new Intl.NumberFormat(lang, { style: "currency", currency: cur, currencyDisplay: "narrowSymbol" }).formatToParts(Number(v || 0));
    let sign = "", sym = "", whole = "", frac = "";
    for (const p of parts) {
      if (p.type === "minusSign") sign = "−";
      else if (p.type === "currency") sym = p.value;
      else if (p.type === "integer" || p.type === "group") whole += p.value;
      else if (p.type === "decimal" || p.type === "fraction") frac += p.value;
    }
    return { sign, sym, whole, frac };
  }
  // "$1,234.50" in en, "$1.234,50" in es/pt; the code is added when the symbol alone is ambiguous
  const money = (v, cur) => {
    cur = cur || currency;
    try { const p = moneyParts(v, cur); return p.sign + p.sym + p.whole + p.frac + ((p.sym === "$" && cur !== "USD") || p.sym === cur ? " " + cur : ""); }
    catch (e) { return `${Number(v || 0).toFixed(2)} ${cur}`; }
  };
  const fmtShort = iso => { if (!iso) return ""; const d = new Date(iso.length <= 10 ? iso + "T12:00:00" : iso); return d.toLocaleDateString(lang, { day: "numeric", month: "short" }); };
  const fmtWhen = iso => new Date(iso).toLocaleString(lang, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  // big amounts as in the design: "$545" with ".00 USD" in smaller type
  function bigMoney(target, v, cur, showCur) {
    cur = cur || currency;
    target.textContent = "";
    let p;
    try { p = moneyParts(v, cur); } catch (e) { target.textContent = money(v, cur); return; }
    const { sign, sym, whole, frac } = p;
    const s = document.createElement("span"); s.textContent = frac;
    target.append(sign + sym + whole, s);
    if (showCur) { const c = document.createElement("small"); c.textContent = " " + cur; target.append(c); }
  }

  const withTimeout = (opts, ms) => {
    const o = Object.assign({}, opts || {});
    if (window.AbortSignal && AbortSignal.timeout) o.signal = AbortSignal.timeout(ms);
    return o;
  };
  async function api(path, opts, ms) {
    const o = Object.assign({}, opts || {});
    if (ws) o.headers = Object.assign({ "X-Workspace": ws }, o.headers || {});  // the business id never goes in a URL
    const r = await fetch(path, withTimeout(o, ms || 45000));
    if (!r.ok) { const e = new Error("http " + r.status); e.status = r.status; throw e; }
    return r.json();
  }
  const post = (path, body, ms) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }, ms);

  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(name) {
    const svg = document.createElementNS(SVGNS, "svg"); svg.setAttribute("class", "ico"); svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS(SVGNS, "use"); use.setAttribute("href", "#i-" + name); svg.appendChild(use);
    return svg;
  }
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }

  let toastTimer = null;
  function toast(msg) { const x = $("toast"); clearTimeout(toastTimer); x.textContent = msg; x.classList.add("show"); toastTimer = setTimeout(() => x.classList.remove("show"), 4800); }

  // ---------- texts ----------
  const PAY_PREFIX = { es: "Págale a ", en: "Pay ", pt: "Pague a " };
  function applyTexts() {
    document.documentElement.lang = lang;
    $("lang").value = lang;
    $$("[data-i18n]").forEach(e => { e.textContent = u(e.dataset.i18n); });
    $$("[data-i18n-html]").forEach(e => { e.innerHTML = u(e.dataset.i18nHtml); });  // fixed UI strings from i18n.js only, never data
    $$("[data-i18n-aria]").forEach(e => { e.setAttribute("aria-label", u(e.dataset.i18nAria)); });
    $("moreClose").setAttribute("aria-label", t("close"));
    $$(".sidenav .workspace-tab, .sidenav .foot-link").forEach(b => { const n = b.querySelector("[data-i18n]") || b.querySelector("span:not(.nav-count)"); if (n) b.title = n.textContent; });
    input.placeholder = t("placeholder"); $("t-label").textContent = t("message"); sendBtn.setAttribute("aria-label", t("send"));
    for (const [id, label] of [["new-conv", u("newConv")], ["readSummary", t("play_brief")], ["closeModal", u("modalClose")]]) { $(id).title = label; $(id).setAttribute("aria-label", label); }
    $("lang-label").textContent = t("lang");
    $("status-hint").textContent = canListen() ? t("hint") : t("v_unsupported");
    $("qr-title").textContent = t("qr_title"); $("qr-close").textContent = t("close");
    $("legend").textContent = "";
    for (const [c, k] of [["#19a37a", "chartIn"], ["#d0574e", "chartOut"], ["#0070e0", "chartBalance"]]) {
      const s = el("span"); const i = el("i"); i.style.background = c; s.append(i, u(k)); $("legend").appendChild(s);
    }
    updateMute(); applyStatus(); setState(state); setTab(tab, true);
    if (briefingData) $("briefing").textContent = briefingData.text;
    if (lastState) render(lastState);
    if (modalConf) fillModal(modalConf);
  }

  function applyStatus() {
    if (!status) return;
    const pill = $("modePill");
    const env = el("b", "", status.paypal_env === "live" ? "Live" : "Sandbox");
    if (status.demo_mode) pill.replaceChildren(el("span", "sandbox-dot"), " ", t("mode_demo"));
    else pill.replaceChildren(el("span", "sandbox-dot"), " PayPal ", env);
    $("voiceReadyText").textContent = status.ai_ready ? (status.demo_mode ? t("mode_demo") : u("voiceReady")) : t("ai_off");
    const notice = !status.ai_ready ? t("ai_off") : status.demo_mode ? t("demo_note") : "";
    $("notice").hidden = !notice; $("notice").textContent = notice;
  }

  function setState(s) {
    state = s;
    studio.dataset.state = s;
    studio.classList.toggle("listening", s === "listening");
    $("status").textContent = t("st_" + s);
    mic.setAttribute("aria-label", s === "listening" ? t("mic_stop") : t("mic_start"));
    mic.setAttribute("aria-pressed", s === "listening" ? "true" : "false");
    sendBtn.disabled = busy;
    if (s === "idle" && queue.length && !busy) setTimeout(flushQueue, 250);
  }

  const chatEnd = () => { const b = $("studioBody"); b.scrollTop = b.scrollHeight; };
  function placeChips() {
    const empty = log.hidden && !pending.length;
    const home = empty ? $("starter") : $("chipsDock");
    if ($("chips").parentElement !== home) home.appendChild($("chips"));
    $("starter").hidden = !empty;
  }
  function add(cls, text) {
    const d = el("div", "msg " + cls, text);
    log.hidden = false; placeChips(); log.appendChild(d); chatEnd(); return d;
  }
  function addAction(a) {
    const label = t("actions")[a.tool] || a.tool;
    const waiting = a.result && a.result.status === "awaiting_owner_confirmation";
    const d = el("div", "act" + (a.error ? " err" : waiting ? " wait" : ""));
    d.append(icon(a.error ? "close" : waiting ? "lock" : "check"), a.error ? `${label}: ${a.error}` : (waiting ? u("pendingAuth") : label));
    log.hidden = false; placeChips(); log.appendChild(d); chatEnd();
  }

  // ---------- voice output: Destiny's own voice, browser voice as fallback ----------
  let speakToken = 0, speakTimer = null, queue = [], unlocked = false, voices = [];
  const loadVoices = () => { try { voices = speechSynthesis.getVoices() || []; } catch (e) { voices = []; } };
  if ("speechSynthesis" in window) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }
  function unlockAudio() {
    if (unlocked) return; unlocked = true;
    try { player.src = SILENT_MP3; player.play().catch(() => {}); } catch (e) { /* ignore */ }
    try { if ("speechSynthesis" in window) { const x = new SpeechSynthesisUtterance(" "); x.volume = 0; speechSynthesis.speak(x); } } catch (e) { /* ignore */ }
  }
  function pickVoice() {
    const exact = SPEECH_LANG[lang].toLowerCase();
    const norm = v => String(v.lang || "").toLowerCase().replace("_", "-");
    return voices.find(v => norm(v) === exact) || voices.find(v => norm(v).startsWith(lang)) || null;
  }
  function browserSpeak(text, token, done) {
    if (!("speechSynthesis" in window)) { done(); return; }
    speechSynthesis.cancel();
    const x = new SpeechSynthesisUtterance(text); const v = pickVoice();
    if (v) x.voice = v; x.lang = v ? v.lang : SPEECH_LANG[lang];
    x.onend = done; x.onerror = done;
    setTimeout(() => { if (token === speakToken) speechSynthesis.speak(x); }, 60);
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
      player.onended = done; player.onerror = fallback;
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
    const b = $("mute"); b.classList.toggle("is-muted", muted);
    const label = muted ? u("unmuteLbl") : u("muteLbl");
    b.setAttribute("aria-pressed", muted ? "true" : "false"); b.setAttribute("aria-label", label); b.title = label;
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
    if (starting || recorder) return;
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
    setTimeout(() => { if (r.state === "recording") r.stop(); }, 20000);
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
    wait.setAttribute("aria-hidden", "true");
    setState("thinking");
    let data;
    try {
      data = await post("/api/chat", { workspace_id: ws, conversation_id: conv, message: text, lang, alternatives: (alts || []).slice(0, 3) }, 100000);
    } catch (e) {
      data = { reply: t(e.status === 429 ? "rate" : e.status ? "server" : "offline"), error: "http" };
    }
    wait.remove();
    epoch++;
    for (const a of data.actions || []) addAction(a);
    let newAuth = null;
    for (const x of data.ui || []) { if (x.qr_png_base64) showQR(x); if (x.confirmation) newAuth = x.confirmation.confirmation_id; }
    const reply = data.reply || t("server");
    add("bot", reply);
    if (data.error === "http" && !(data.actions || []).length) input.value = text;
    if (data.error === "conversation_full") newConversation(true);
    busy = false;
    speak(reply, data.speech_id);
    if (newAuth) wantOpen = newAuth;  // the review dialog is the visible gate: opened as soon as the card is rendered
    await refresh();
  }
  $("f").addEventListener("submit", e => { e.preventDefault(); const text = input.value; cancelListening(); send(text); });
  $$("#chips .prompt-chip").forEach(b => b.addEventListener("click", () => { cancelListening(); send(u(b.dataset.cmd)); }));

  // ---------- authorization gate ----------
  let pending = [], modalConf = null, lastFocus = null;
  const KIND_TYPE = { send_invoice: "modalInvoiceType", cancel_invoice: "modalCancelType", pay_bill: "modalPayoutType" };
  const KIND_TITLE = { send_invoice: "modalTitleInvoice", cancel_invoice: "modalTitleCancel", pay_bill: "modalTitlePayout" };
  const amountOf = c => { const d = c.details || {}; return d.total != null ? d.total : d.amount; };
  const whoOf = c => { const d = c.details || {}; return d.customer || d.payee || ""; };
  const conceptOf = c => {
    const d = c.details || {};
    if ((d.items || []).length) return d.items.map(it => `${it.quantity} × ${it.name} · ${money(it.unit_price, d.currency)}`).join(" · ");
    return d.description || "—";
  };

  let shownCards = 0;
  function renderAuthCards() {
    const box = $("cards"); box.textContent = "";
    const count = $("countAuth"); count.hidden = !pending.length; count.textContent = String(pending.length);
    for (const c of pending) {
      const card = el("div", "auth-card");
      const ic = el("span", "ac-icon"); ic.appendChild(icon("lock"));
      const txt = el("div"); txt.append(el("b", "", u(KIND_TYPE[c.kind])), el("small", "", `${whoOf(c)} · ${(c.details || {}).email || ""}`));
      const amt = el("span", "ac-amount", money(amountOf(c), (c.details || {}).currency));
      const actions = el("div", "ac-actions");
      const st = el("span", "ac-state", u("pendingAuth").toUpperCase());
      const btn = el("button", "ac-review"); btn.type = "button"; btn.append(icon("shield"), u("reviewCta"));
      btn.addEventListener("click", () => openModal(c));
      actions.append(st, btn);
      card.append(ic, txt, amt, actions);
      box.appendChild(card);
    }
    placeChips();
    if (pending.length > shownCards) chatEnd();
    shownCards = pending.length;
  }

  function renderSecurityCard() {
    const c = pending[0];
    $("secCard").classList.toggle("is-example", !c);
    if (c) {
      $("secSub").textContent = u(KIND_TYPE[c.kind]);
      $("secBadge").textContent = u("securityPending"); $("secBadge").classList.remove("none");
      $("secTo").textContent = whoOf(c); $("secEmail").textContent = (c.details || {}).email || "—";
      $("secAmount").textContent = money(amountOf(c), (c.details || {}).currency);
    } else {
      $("secSub").textContent = u("securityEmpty");
      $("secBadge").textContent = u("securityExample"); $("secBadge").classList.add("none");
      $("secTo").textContent = "Textiles Andinos SAC"; $("secEmail").textContent = "pagos@textilesandinos.example.com";
      $("secAmount").textContent = money(180, "USD");
    }
    $("secFoot").textContent = u("securityNoMove");
  }

  function fillModal(c) {
    const d = c.details || {};
    $("modalTitle").textContent = u(KIND_TITLE[c.kind]);
    bigMoney($("modalAmountValue"), amountOf(c), d.currency, true);
    $("modalOperation").textContent = u(KIND_TYPE[c.kind]);
    $("modalRecipientValue").textContent = whoOf(c);
    $("modalEmailValue").textContent = d.email || "—";
    $("modalConceptValue").textContent = conceptOf(c);
    $("modalReadback").textContent = c.readback || "";
    $("confirmApproval").querySelector("span").textContent = `${u("modalConfirm")} ${money(amountOf(c), d.currency)}`;
    $("modalExpires").textContent = c.expires_at ? u("modalExpires", { t: new Date(c.expires_at * 1000).toLocaleTimeString(lang, { hour: "2-digit", minute: "2-digit" }) }) : "";
  }
  function openModal(c) {
    if (!c) return;
    modalConf = c;
    lastFocus = document.activeElement;
    fillModal(c);
    $("reviewCheckbox").checked = false; $("confirmApproval").disabled = true; $("rejectApproval").disabled = false;
    $("approvalBackdrop").hidden = false; document.body.classList.add("modal-open");
    document.querySelector(".site").inert = true;
    $("closeModal").focus();
  }
  let wantOpen = null;
  function closeModal() {
    if ($("approvalBackdrop").hidden) return;
    $("approvalBackdrop").hidden = true; document.body.classList.remove("modal-open"); modalConf = null;
    document.querySelector(".site").inert = false;
    if (lastFocus && typeof lastFocus.focus === "function") lastFocus.focus();
  }
  async function decide(approve) {
    const c = modalConf; if (!c) return;
    if (approve && !$("reviewCheckbox").checked) return;
    unlockAudio(); cancelListening(); stopSpeaking();
    $("confirmApproval").disabled = true; $("rejectApproval").disabled = true;
    try {
      const body = { workspace_id: ws, conversation_id: conv, confirmation_id: c.id, approve, lang };
      if (approve) body.fingerprint = c.fingerprint;  // approves this exact operation and nothing else
      const r = await post("/api/confirm", body, 100000);  // a payout can wait on PayPal's retries
      epoch++;
      closeModal();
      add(r.done ? "bot alert" : "bot", r.reply);
      toast(approve ? (r.done ? u("toastAuthorized") : r.reply) : u("toastRejected"));
      speak(r.reply, r.speech_id);
    } catch (e) {
      toast(t(e.status === 429 ? "rate" : "server"));
      $("confirmApproval").disabled = !$("reviewCheckbox").checked;
    } finally { $("rejectApproval").disabled = false; }
    refresh();
  }
  $("reviewCheckbox").addEventListener("change", e => { $("confirmApproval").disabled = !e.target.checked; });
  $("confirmApproval").addEventListener("click", () => decide(true));
  $("rejectApproval").addEventListener("click", () => decide(false));
  $("closeModal").addEventListener("click", closeModal);
  $("approvalBackdrop").addEventListener("click", e => { if (e.target === $("approvalBackdrop")) closeModal(); });
  $("readApproval").addEventListener("click", async () => {
    if (!modalConf) return;
    unlockAudio();
    try { const r = await api(`/api/confirmation/${encodeURIComponent(modalConf.id)}/readback?lang=${lang}`); speak(r.text, r.speech_id); }
    catch (e) { speak(modalConf.readback || "", null); }
  });

  // ---------- dashboard ----------
  const TABS = ["overview", "invoices", "payouts", "forecast", "reminders", "contacts", "memory", "activity", "security"];
  let tab = TABS.includes(store.get("dp_tab")) ? store.get("dp_tab") : "overview";
  const app = $("app"), phone = window.matchMedia("(max-width: 1023px)");
  function setTab(name, quiet) {
    if (!TABS.includes(name)) return;
    tab = name; if (!quiet) store.set("dp_tab", tab);
    $$(".workspace-tab").forEach(b => { const on = b.dataset.tab === tab; b.classList.toggle("active", on); b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; });
    $$(".ws-panel").forEach(p => { p.hidden = p.dataset.panel !== tab; });
    const cap = tab[0].toUpperCase() + tab.slice(1);
    $("workspaceTitle").textContent = u("tabTitle" + cap); $("wsKicker").textContent = u("tabKicker" + cap);
    syncNav();
  }
  function syncNav() {
    const view = app.dataset.mview === "assistant" ? "assistant" : tab;
    const primary = ["overview", "invoices", "payouts", "assistant"];
    $$(".bn-btn").forEach(b => b.classList.toggle("active", b.dataset.view === view || (b.dataset.view === "more" && !primary.includes(view))));
    $$(".more-item[data-view]").forEach(b => b.classList.toggle("active", b.dataset.view === view));
  }
  function setMView(v) { app.dataset.mview = v; syncNav(); }
  function goView(name) {
    closeMore();
    if (name === "assistant") { setMView("assistant"); return; }
    setTab(name); setMView("dash"); $("main").scrollTop = 0; if (phone.matches) window.scrollTo(0, 0);
  }
  function openMore() { $("moreSheet").hidden = false; $("moreBtn").setAttribute("aria-expanded", "true"); $$("#moreSheet .more-item")[0].focus(); }
  function closeMore() { if ($("moreSheet").hidden) return; $("moreSheet").hidden = true; $("moreBtn").setAttribute("aria-expanded", "false"); }
  $$(".bn-btn").forEach(b => b.addEventListener("click", () => { if (b.dataset.view === "more") openMore(); else goView(b.dataset.view); }));
  $$(".more-item[data-view]").forEach(b => b.addEventListener("click", () => goView(b.dataset.view)));
  $("moreClose").addEventListener("click", closeMore);
  $("moreSheet").addEventListener("click", e => { if (e.target === $("moreSheet")) closeMore(); });
  $$(".workspace-tab").forEach(b => {
    b.addEventListener("click", () => { setTab(b.dataset.tab); setMView("dash"); });
    b.addEventListener("keydown", e => {
      if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft"].includes(e.key)) return;
      e.preventDefault();
      const i = TABS.indexOf(tab) + (e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : -1);
      setTab(TABS[(i + TABS.length) % TABS.length]); $$(".workspace-tab").find(x => x.dataset.tab === tab).focus();
    });
  });

  function metric(label, iconName, value, sub, subCls) {
    const tile = el("div", "metric-tile");
    const top = el("div", "metric-top"); const ic = el("span", "metric-icon"); ic.appendChild(icon(iconName));
    top.append(el("span", "", label), ic);
    const v = el("div", "metric-value"); bigMoney(v, value);
    tile.append(top, v, el("small", subCls || "", sub || " "));
    return tile;
  }
  function renderMetrics(o) {
    const box = $("metrics"); box.textContent = "";
    const lateSum = (o.bills_overdue || []).reduce((n, b) => n + b.amount, 0);
    const top = o.top_expense_categories && o.top_expense_categories[0];
    const next = (o.next_bills || []).find(b => b.due_date >= (o.today || ""));
    box.append(
      metric(t("kpi_receivable"), "receipt", o.receivable, o.overdue_invoices.length ? t("overdue_n")(o.overdue_invoices.length) : t("open_n")(o.open_invoices), o.overdue_invoices.length ? "metric-alert" : ""),
      metric(t("kpi_bills"), "wallet", o.bills_next_7_days - lateSum, o.bills_overdue.length ? t("overdue_bills")(o.bills_overdue.length, money(lateSum)) : (next ? t("next_bill")(next.payee, fmtShort(next.due_date)) : ""), o.bills_overdue.length ? "metric-alert" : ""),
      metric(t("kpi_income"), "arrowup", o.income, t("net")(money(o.net))),
      metric(t("kpi_expense"), "chart", o.expense, top ? u("metricExpensesTop", { cat: t("cat")[top.category] || top.category, amount: money(top.amount) }) : ""),
    );
  }

  function renderAreaChart(days, projected) {
    bigMoney($("projValue"), projected);
    const svg = $("areaChart"), W = 680, H = 230, top = 22, bottom = 18;
    let run = 0; const cum = days.map(d => (run += d.in - d.out));
    const lo = Math.min(0, ...cum), hi = Math.max(1, ...cum);
    const x = i => 2 + i * (W - 4) / Math.max(1, days.length - 1);
    const y = v => top + (hi - v) * (H - top - bottom) / (hi - lo || 1);
    const pts = cum.map((v, i) => `${x(i).toFixed(1)} ${y(v).toFixed(1)}`);
    const line = "M" + (pts.length ? pts.join(" L") : `2 ${y(0)} L${W - 2} ${y(0)}`);
    const zero = y(0).toFixed(1);
    const lastX = cum.length ? x(cum.length - 1) : W - 2, lastY = cum.length ? y(cum[cum.length - 1]) : y(0);
    // only numbers computed here go into this markup, never text from data
    svg.innerHTML = `<defs><linearGradient id="areaFade" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#1688ee" stop-opacity=".19"/><stop offset="100%" stop-color="#1688ee" stop-opacity="0"/></linearGradient></defs>`
      + `<path class="chart-gridline" d="M0 35H680M0 90H680M0 145H680M0 200H680"/>`
      + `<path d="M0 ${zero}H680" stroke="#c9d6e6" stroke-dasharray="4 5"/>`
      + `<path d="${line} L${lastX.toFixed(1)} ${H} L2 ${H}Z" fill="url(#areaFade)"/>`
      + `<path d="${line}" fill="none" stroke="#0070e0" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>`
      + `<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="6" fill="#0070e0" stroke="#fff" stroke-width="3"/>`;
    svg.setAttribute("aria-label", u("chartTitle"));
    const axis = $("areaAxis"); axis.textContent = "";
    if (days.length) for (const i of [0, 7, 14, 21, days.length - 1]) axis.appendChild(el("span", "", fmtShort(days[Math.min(i, days.length - 1)].date).toUpperCase()));
  }

  function renderBarsChart(days) {
    const svg = $("chart"); const W = 680, H = 240, pad = 6, mid = H * 0.55;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const max = Math.max(1, ...days.map(d => Math.max(d.in, d.out)));
    let run = 0; const pts = [];
    const cum = days.map(d => (run += d.in - d.out));
    const cmax = Math.max(1, ...cum.map(Math.abs));
    const bw = (W - pad * 2) / Math.max(1, days.length);
    let out = `<line x1="0" y1="${mid}" x2="${W}" y2="${mid}" stroke="#e4ebf3"/>`;
    days.forEach((d, i) => {
      const bx = pad + i * bw + bw * 0.2, w = bw * 0.6;
      if (d.in) { const h = (d.in / max) * (mid - 14); out += `<rect x="${bx}" y="${mid - h}" width="${w}" height="${h}" rx="3" fill="#19a37a"/>`; }
      if (d.out) { const h = (d.out / max) * (H - mid - 14); out += `<rect x="${bx}" y="${mid}" width="${w}" height="${h}" rx="3" fill="#d0574e"/>`; }
      const yy = cum[i] >= 0 ? mid - (cum[i] / cmax) * (mid - 12) : mid + (-cum[i] / cmax) * (H - mid - 10);
      pts.push(`${pad + i * bw + bw / 2},${yy}`);
    });
    out += `<polyline points="${pts.join(" ")}" fill="none" stroke="#0070e0" stroke-width="2.4"/>`;
    svg.innerHTML = out;
    svg.setAttribute("aria-label", u("chartTitle"));
    const axis = $("barsAxis"); axis.textContent = "";
    if (days.length) for (const i of [0, 7, 14, 21, days.length - 1]) axis.appendChild(el("span", "", fmtShort(days[Math.min(i, days.length - 1)].date).toUpperCase()));
  }

  function renderDue(s) {
    const today = s.overview.today || "";
    const items = [];
    for (const b of s.bills) if (b.status === "pending") items.push({ who: b.payee, amount: b.amount, date: b.due_date, kind: b.due_date < today ? "overdue" : "pay" });
    for (const i of s.invoices) if (OPEN.has(i.status)) items.push({ who: i.customer, amount: i.due, date: i.due_date, kind: i.overdue ? "overdue" : "collect" });
    items.sort((a, b) => (a.kind === "overdue" ? 0 : 1) - (b.kind === "overdue" ? 0 : 1) || String(a.date).localeCompare(String(b.date)));
    const list = $("dueList"); list.textContent = "";
    $("dueCount").textContent = String(items.length).padStart(2, "0");
    if (!items.length) { list.appendChild(el("div", "due-empty", u("dueEmpty"))); return; }
    const SHOWN = 4;
    for (const it of items.slice(0, SHOWN)) {
      const row = el("div", "due-item");
      const dot = el("span", "due-bullet " + (it.kind === "overdue" ? "red" : it.kind === "pay" ? "orange" : "blue"));
      const mid = el("div"); mid.append(el("b", "", it.who), el("small", "", u(it.kind === "overdue" ? "dueOverdue" : it.kind === "pay" ? "duePay" : "dueCollect", { date: fmtShort(it.date) })));
      row.append(dot, mid, el("strong", "", money(it.amount)));
      list.appendChild(row);
    }
    if (items.length > SHOWN) list.appendChild(el("div", "due-more", u("dueMore", { n: items.length - SHOWN })));
  }

  function renderLedger(moves) {
    const body = $("ledgerBody"); body.textContent = "";
    if (!moves.length) { const tr = el("tr", "empty-row"); const td = el("td", "", u("noMovements")); td.colSpan = 5; tr.appendChild(td); body.appendChild(tr); return; }
    for (const m of moves.slice(0, 8)) {
      const tr = el("tr");
      const label = m.contact || (m.kind === "income" ? t("cat").sales : (t("cat")[m.category] || m.category || "—"));
      const who = el("td"); who.appendChild(el("b", "", label.charAt(0).toUpperCase() + label.slice(1)));
      const amt = el("td", "table-amount " + (m.kind === "income" ? "pos" : "neg"), (m.kind === "income" ? "+" : "−") + money(m.amount, m.currency));
      // a Yape or cash sale is only recorded here: it is never shown as processed by PayPal
      const method = el("td"); method.appendChild(el("span", "table-status " + (m.method === "PAYPAL" ? "sent" : "demo"), (u("method")[m.method] || m.method || "—")));
      tr.append(who, el("td", "", m.description), el("td", "", fmtShort(new Date(m.at * 1000).toISOString())), amt, method);
      body.appendChild(tr);
    }
  }

  function renderRec(s) {
    const o = s.overview;
    const ob = (o.bills_overdue || [])[0];
    // a deterministic, data-grounded first step (the assistant gives the full advice on request)
    const oi = (o.overdue_invoices || [])[0], nb = (o.next_bills || []).find(b => b.due_date >= (o.today || ""));
    $("recText").textContent = ob ? u("recOverdueBill", { payee: ob.payee, amount: money(ob.amount) })
      : oi ? u("recOverdueInvoice", { name: oi.customer, amount: money(oi.due), date: fmtShort(oi.due_date) })
      : nb ? u("recNext", { payee: nb.payee, amount: money(nb.amount), date: fmtShort(nb.due_date) }) : u("recClear");
  }
  function renderCounts(s) {
    const set = (id, n) => { const x = $(id); x.hidden = !n; x.textContent = String(n); };
    set("countInvoices", s.invoices.filter(i => OPEN.has(i.status)).length);
    set("countPayouts", s.bills.filter(x => x.status === "pending").length);
  }

  function renderAging(ag) {
    const box = $("aging"); box.textContent = "";
    const keys = ["not_due", "d1_30", "d31_60", "d61_plus"];
    const total = ag ? keys.reduce((n, k) => n + (ag[k] || 0), 0) : 0;
    box.hidden = !total; if (!total) return;
    const bar = el("div", "aging-bar"), legend = el("div", "aging-legend");
    for (const k of keys) {
      const v = ag[k] || 0;
      if (v) { const seg = el("span", "seg " + k); seg.style.width = (100 * v / total) + "%"; seg.title = `${t("aging")[k]}: ${money(v)}`; bar.appendChild(seg); }
      const item = el("span", "item" + (v ? "" : " zero")); item.append(el("i", k), el("span", "", t("aging")[k]), el("strong", "", money(v)));
      legend.appendChild(item);
    }
    box.append(el("div", "aging-head", u("agingLabel").toUpperCase()), bar, legend);
  }

  // AG Grid tables
  const grids = {};
  const gridTheme = () => agGrid.themeQuartz.withParams({
    fontFamily: "Inter, system-ui, sans-serif", fontSize: 13, backgroundColor: "#ffffff", foregroundColor: "#0a2448",
    headerBackgroundColor: "#f6f9fd", headerTextColor: "#72839a", headerFontSize: 11.5, headerFontWeight: 750,
    borderColor: "#e4ebf3", rowBorder: { color: "#eef2f7" }, wrapperBorderRadius: 14, accentColor: "#0070e0", rowHoverColor: "#f2f8ff", cellHorizontalPadding: 12,
  });
  function pill(text, cls) { return el("span", "pill " + (cls || ""), text); }
  function statusPill(p) {
    const r = p.data; if (!r) return "";
    const key = r.overdue ? "overdue" : r.unclaimed ? "unclaimed" : r.status;
    const cls = r.overdue ? "bad" : r.unclaimed ? "info" : PAID.has(r.status) || r.status === "paid" ? "ok" : r.status === "processing" ? "info" : OPEN.has(r.status) || r.status === "pending" ? "warn" : BAD.has(r.status) ? "bad" : "";
    return pill(t("status")[key] || r.status, cls);
  }
  function invoiceActions(p) {
    const r = p.data; const box = el("span"); if (!r) return box;
    if (r.payer_link) { const a = el("a", "cell-btn", t("pay_link")); a.href = r.payer_link; a.target = "_blank"; a.rel = "noopener noreferrer"; box.appendChild(a); }
    if (status && status.demo_mode && OPEN.has(r.status)) {
      const b = el("button", "cell-btn", t("simulate")); b.type = "button";
      b.addEventListener("click", async () => { b.disabled = true; try { await api(`/api/demo/pay/${encodeURIComponent(r.id)}`, { method: "POST" }); } catch (e) { /* refresh shows it */ } refresh(); });
      box.appendChild(b);
    }
    return box;
  }
  function billActions(p) {
    const r = p.data; const box = el("span"); if (!r || r.status !== "pending") return box;
    const b = el("button", "cell-btn", t("pay_paypal")); b.type = "button";
    b.addEventListener("click", () => { toAssistant(); send(t("cmd_pay_bill")(r.payee, r.id)); });
    box.appendChild(b); return box;
  }
  function twoLines(p) {
    const r = p.data; const box = el("div", "two-lines"); if (!r) return box;
    box.append(el("strong", "", r.payee), el("span", "", [r.description, t("recurrence")[r.recurrence]].filter(Boolean).join(" · ")));
    return box;
  }
  const narrow = () => window.matchMedia && matchMedia("(max-width: 560px)").matches;  // phones: dates are in the due list
  function columns(name) {
    const m = p => p.value == null ? "" : money(p.value, p.data && p.data.currency);
    const d = p => fmtShort(p.value);
    if (name === "invoices") return [
      { field: "customer", headerName: t("col_customer"), flex: 1.3, minWidth: 120 },
      { field: "total", headerName: t("col_total"), valueFormatter: m, minWidth: 110, flex: 0.9 },
      { field: "due_date", headerName: t("col_due_date"), valueFormatter: d, minWidth: 90, flex: 0.7, hide: narrow() },
      { field: "status", headerName: t("col_status"), cellRenderer: statusPill, minWidth: 132, flex: 1 },
      { headerName: t("col_actions"), cellRenderer: invoiceActions, width: status && status.demo_mode ? 256 : 150, sortable: false },
    ];
    if (name === "bills") return [
      { field: "payee", headerName: t("col_payee"), flex: 2, minWidth: 150, cellRenderer: twoLines },
      { field: "amount", headerName: t("col_amount"), valueFormatter: m, minWidth: 112, flex: 0.9 },
      { field: "due_date", headerName: t("col_due_date"), valueFormatter: d, minWidth: 96, flex: 0.8, sort: "asc", hide: narrow() },
      { field: "status", headerName: t("col_status"), cellRenderer: statusPill, minWidth: 130, flex: 0.9 },
      { headerName: t("col_actions"), cellRenderer: billActions, width: 164, sortable: false },
    ];
    return [
      { field: "name", headerName: t("col_name"), flex: 1.2, minWidth: 140 },
      { field: "email", headerName: t("col_email"), flex: 1.6, minWidth: 180 },
      { field: "kind", headerName: t("col_kind"), valueFormatter: p => t("kind")[p.value] || p.value, minWidth: 110, flex: 0.8 },
      { field: "notes", headerName: t("col_notes"), flex: 2, minWidth: 200 },
    ];
  }
  function setGrid(name, rows) {
    const holder = $("grid-" + name);
    if (grids[name] && grids[name].__lang !== lang) { grids[name].destroy(); delete grids[name]; }
    if (!grids[name]) {
      grids[name] = agGrid.createGrid(holder, { theme: gridTheme(), columnDefs: columns(name), rowData: rows, getRowId: p => String(p.data.id || p.data.name), rowHeight: name === "bills" ? 56 : 46,
        defaultColDef: { sortable: true, filter: false, resizable: true }, animateRows: true, localeText: { noRowsToShow: t("no_rows"), loadingOoo: "…" } });
      grids[name].__lang = lang; grids[name].__json = JSON.stringify(rows);
    } else {
      const json = JSON.stringify(rows);
      if (grids[name].__json !== json) { grids[name].setGridOption("rowData", rows); grids[name].__json = json; }
    }
    // a short table fits its rows; a long one scrolls inside the view
    const fit = rows.length <= 14;
    if (grids[name].__fit !== fit) { grids[name].setGridOption("domLayout", fit ? "autoHeight" : "normal"); holder.classList.toggle("fit", fit); grids[name].__fit = fit; }
  }
  function renderList(id, items, empty, fmt) {
    const ul = $(id); ul.textContent = "";
    if (!items.length) { ul.appendChild(el("li", "empty", empty)); return; }
    for (const it of items) ul.appendChild(fmt(it));
  }
  function li(when, text, ic) {
    const x = el("li");
    if (ic) { const s = el("span", "li-ic"); s.appendChild(icon(ic)); x.appendChild(s); }
    const body = el("div");
    if (when) body.appendChild(el("div", "when", when));
    body.appendChild(el("div", "", text)); x.appendChild(body); return x;
  }

  function render(s) {
    lastState = s; currency = s.business.currency;
    $("bizName").textContent = s.business.name;
    $("bizSub").textContent = s.business.sample ? u("workspaceSub") : u("workspaceSub").split("·")[0].trim();
    renderMetrics(s.overview); renderAreaChart(s.cashflow, s.overview.projected_30_days); renderBarsChart(s.cashflow);
    renderDue(s); renderLedger(s.movements || []); renderRec(s); renderCounts(s); renderAging(s.overview.receivable_aging);
    if (window.agGrid) { setGrid("invoices", s.invoices); setGrid("bills", s.bills); setGrid("contacts", s.contacts); }
    renderList("reminders", s.reminders, t("empty_reminders"), r => li(fmtWhen(r.due_at), r.text, r.kind === "remind_customer" ? "mail" : "bell"));
    renderList("activity", s.activity, t("empty_activity"), a => li(new Date(a.at * 1000).toLocaleString(lang, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }), a.text, "clock"));
    renderList("memory", s.facts, t("empty_memory"), f => li("", f, "spark"));
    pending = s.confirmations || [];
    renderAuthCards(); renderSecurityCard();
    if (wantOpen) { const c = pending.find(x => x.id === wantOpen); if (c) { wantOpen = null; if (!modalConf) openModal(c); } }
    if (modalConf) {
      const still = pending.find(c => c.id === modalConf.id && c.fingerprint === modalConf.fingerprint);
      if (!still) closeModal();  // decided elsewhere (by voice) or replaced by a changed proposal
    }
  }

  let refreshing = false, firstLoad = true, epoch = 0, again = false;
  async function refresh() {
    if (!ws) { try { await createWorkspace(); } catch (e) { /* shown at start */ } return; }
    if (refreshing) { again = true; return; }
    refreshing = true;
    const myEpoch = epoch;
    try {
      const s = await api(`/api/state?lang=${lang}&since=${lastAlert}`, undefined, 15000);
      if (myEpoch !== epoch) { again = true; return; }
      render(s);
      if (!firstLoad) for (const a of s.alerts) { add("bot alert", a.text); toast(a.text); sayWhenIdle(a.text, a.speech_id); }
      firstLoad = false;
      lastAlert = Math.max(lastAlert, s.last_alert_id || 0);
    } catch (e) {
      if (e.status === 404) { refreshing = false; await createWorkspace(); }
    } finally {
      refreshing = false;
      if (again) { again = false; setTimeout(refresh, 50); }
    }
  }

  function showQR(x) {
    const body = $("qr-body"); body.textContent = "";
    const img = new Image(); img.alt = t("qr_title");
    img.onload = () => { if (img.naturalWidth < 20) { body.textContent = ""; body.appendChild(el("p", "", t("qr_demo"))); } };
    img.src = "data:image/png;base64," + x.qr_png_base64; body.appendChild(img);
    const linkBox = $("qr-link"); linkBox.textContent = "";
    const row = lastState && lastState.invoices.find(i => i.id === x.invoice_id);
    if (row && row.payer_link) { const a = el("a", "cell-btn", t("pay_link")); a.href = row.payer_link; a.target = "_blank"; a.rel = "noopener noreferrer"; linkBox.appendChild(a); }
    try { $("qr").showModal(); } catch (e) { $("qr").setAttribute("open", ""); }
  }

  // ---------- sessions ----------
  async function createWorkspace() {
    const tz = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "America/Lima";
    ws = null;
    const r = await post("/api/workspace", { lang, tz, sample: true });
    ws = r.workspace_id; store.set("dp_ws", ws); lastAlert = 0; firstLoad = true; pending = []; renderAuthCards();
    await refresh();
  }
  function newConversation(keepLog) {
    conv = uuid(); store.set("dp_conv", conv, true);
    if (!keepLog) { log.textContent = ""; log.hidden = true; placeChips(); }
  }
  async function briefing() {
    try { const b = await api(`/api/briefing?lang=${lang}`); briefingData = b; $("briefing").textContent = b.text; }
    catch (e) { /* the briefing is optional */ }
  }
  $("readSummary").addEventListener("click", () => { unlockAudio(); if (briefingData) speak(briefingData.text, briefingData.speech_id); });

  $("lang").addEventListener("change", async e => {
    if (busy) { e.target.value = lang; return; }
    cancelListening(); queue = []; stopSpeaking(); closeModal();
    lang = I18N[e.target.value] ? e.target.value : "en"; store.set("dp_lang", lang);
    newConversation(false);  // the assistant's language is fixed per conversation; memory stays
    briefingData = null; applyTexts(); await refresh(); briefing();
  });
  $("mute").addEventListener("click", () => { muted = !muted; store.set("dp_muted", muted ? "1" : "0"); if (muted) { queue = []; stopSpeaking(); } updateMute(); });
  $("new-conv").addEventListener("click", () => { if (busy) return; cancelListening(); queue = []; stopSpeaking(); newConversation(false); briefing(); });
  $("new-biz").addEventListener("click", async () => {
    if (busy || !confirm(t("new_biz_q"))) return;
    cancelListening(); queue = []; stopSpeaking(); closeModal(); newConversation(false); await createWorkspace(); briefing();
  });
  const toAssistant = () => { if (phone.matches) setMView("assistant"); };
  $("askPriority").addEventListener("click", () => { toAssistant(); send(u("cmdPriority")); });
  $("workspacePayout").addEventListener("click", () => { toAssistant(); input.value = PAY_PREFIX[lang] || ""; setTimeout(() => input.focus(), 350); });
  $("dueAction").addEventListener("click", () => { if (pending[0]) openModal(pending[0]); else { toAssistant(); send(u("cmdPriority")); } });
  $("safetyReview").addEventListener("click", () => { if (pending[0]) openModal(pending[0]); else { toAssistant(); setTimeout(() => input.focus(), 350); } });
  $("moreNewBiz").addEventListener("click", () => { closeMore(); $("new-biz").click(); });

  document.addEventListener("keydown", e => {
    if (!$("approvalBackdrop").hidden) {
      if (e.key === "Escape") { e.preventDefault(); closeModal(); return; }
      if (e.key === "Tab") {  // keep focus inside the authorization dialog
        const f = $$("#approvalBackdrop button:not([disabled]), #approvalBackdrop input:not([disabled])");
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
      return;
    }
    if (e.key === "Escape" && !$("moreSheet").hidden) { closeMore(); $("moreBtn").focus(); return; }
    if (e.key === "Escape") { cancelListening(); queue = []; stopSpeaking(); }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); toAssistant(); setTimeout(() => input.focus(), 0); }
  });

  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 5000);

  // ---------- start ----------
  (async () => {
    store.set("dp_conv", conv, true);
    applyTexts();
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
    if (ws) briefing();
  })();
})();
