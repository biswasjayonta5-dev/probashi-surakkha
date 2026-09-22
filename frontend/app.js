/* Bangla-first UI. Design rules enforced here, not just in the backend:
 *  - the tool never renders a "safe" verdict; it renders found / not found + date
 *  - every screen keeps the sourcing line, the helpline and the disclaimer visible
 *  - voice input/output is offered wherever text entry is, because literacy
 *    cannot be assumed (plan §4)
 */

const state = { file: null, meta: null, texts: {} };

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ------------------------------------------------------------------ tabs */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    ["licence", "contract", "fee", "ask"].forEach((name) => {
      $("tab-" + name).hidden = name !== tab.dataset.tab;
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
});

/* --------------------------------------------------------------- helpers */
async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.message_bn || "সমস্যা হয়েছে। আবার চেষ্টা করুন।");
  return data;
}

function speak(text) {
  if (!("speechSynthesis" in window)) return;
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "bn-BD";
  utterance.rate = 0.9;
  const bengali = window.speechSynthesis.getVoices().find((v) => v.lang.startsWith("bn"));
  if (bengali) utterance.voice = bengali;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(utterance);
}

function speakButton(text) {
  return `<div class="speak"><button type="button" data-speak>🔊 উত্তরটি শুনুন</button></div>`;
}

function bindSpeak(container, text) {
  const button = container.querySelector("[data-speak]");
  if (button) button.addEventListener("click", () => speak(text));
}

function disclaimer(extra) {
  return `<div class="disclaimer">${esc(state.texts.disclaimer_bn || "")} ${esc(extra || "")}</div>`;
}

function flagList(flags) {
  if (!flags || !flags.length) return `<p class="muted">স্বাভাবিক নিয়মে কোনো সতর্কতা পাওয়া যায়নি।</p>`;
  return `<ul class="flags">${flags.map((f) => `
    <li class="flag ${esc(f.severity)}">
      <div class="t">${esc(f.label_bn)}</div>
      <div class="r">${esc(f.reason_bn)}</div>
      ${f.source_ref ? `<div class="s">সূত্র: ${esc(f.source_ref)}</div>` : ""}
    </li>`).join("")}</ul>`;
}

/* ------------------------------------------------------- voice (Bangla) */
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;

document.querySelectorAll("[data-mic]").forEach((button) => {
  const target = $(button.dataset.mic);
  if (!SpeechRecognition) {
    button.disabled = true;
    button.title = "এই ব্রাউজারে বাংলা ভয়েস সাপোর্ট নেই — লিখে দিন";
    return;
  }
  button.addEventListener("click", () => {
    const recognition = new SpeechRecognition();
    recognition.lang = "bn-BD";
    recognition.interimResults = false;
    recognition.maxAlternatives = 1;
    button.classList.add("listening");
    recognition.onresult = (event) => {
      target.value = event.results[0][0].transcript;
      target.dispatchEvent(new Event("input"));
    };
    recognition.onerror = () => { button.classList.remove("listening"); };
    recognition.onend = () => { button.classList.remove("listening"); };
    recognition.start();
  });
});

/* ----------------------------------------------------------- 1. licence */
$("licence-go").addEventListener("click", async () => {
  const query = $("licence-query").value.trim();
  const box = $("licence-result");
  box.hidden = false;
  if (!query) { box.innerHTML = `<p class="error">আরএল নম্বর বা নাম লিখুন।</p>`; return; }
  box.innerHTML = `<p class="muted">খোঁজা হচ্ছে…</p>`;
  try {
    const data = await api("/api/licence", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ query }),
    });
    const cls = data.status === "found" ? "verdict-found"
      : data.status === "not_found" ? "verdict-notfound" : "verdict-unknown";
    const rows = (data.matches || []).map((m) => `
      <li><span class="sec">${esc(m.record.name)}</span> — আরএল: ${esc(m.record.rl_number)},
      অবস্থা: ${esc(m.record.status)}${m.record.valid_until ? `, মেয়াদ: ${esc(m.record.valid_until)}` : ""}
      ${m.match_type === "fuzzy_name" ? ` <span class="muted">(নামের কাছাকাছি মিল, ${esc(m.score)}%)</span>` : ""}</li>`).join("");
    box.innerHTML = `
      <h3>ফলাফল</h3>
      <div class="verdict ${cls}">${esc(data.message_bn)}</div>
      ${rows ? `<ul class="citations">${rows}</ul>` : ""}
      <div class="sourceline">তথ্যের তারিখ: ${esc(data.data_date || "অজানা")} · সূত্র: ${esc(data.source)}${
        data.is_official_data ? "" : " · ⚠️ ডেমো ডেটা"}</div>
      ${(data.notes_bn || []).map((n) => `<p class="muted">• ${esc(n)}</p>`).join("")}
      ${disclaimer(state.texts.never_safe_note_bn)}
      ${speakButton()}`;
    bindSpeak(box, data.message_bn);
  } catch (error) {
    box.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
});

/* ---------------------------------------------------------- 2. contract */
$("consent-ok").addEventListener("change", (event) => {
  $("contract-pick").disabled = !event.target.checked;
});

$("contract-pick").addEventListener("click", () => $("contract-file").click());

$("contract-file").addEventListener("change", (event) => {
  state.file = event.target.files[0] || null;
  const info = $("contract-fileinfo");
  if (!state.file) { info.hidden = true; $("contract-go").disabled = true; return; }
  const sizeMb = (state.file.size / 1048576).toFixed(2);
  info.hidden = false;
  info.textContent = `নির্বাচিত ফাইল: ${state.file.name} (${sizeMb} MB)`;
  $("contract-go").disabled = false;
});

$("contract-go").addEventListener("click", async () => {
  const box = $("contract-result");
  box.hidden = false;
  box.innerHTML = `<p class="muted">পরীক্ষা করা হচ্ছে… (২০ সেকেন্ড পর্যন্ত লাগতে পারে)</p>`;

  const answers = {};
  const mapping = {
    "a-agency-name": "agency_name",
    "a-agency-rl": "agency_rl",
    "a-visa": "visa_type",
  };
  Object.entries(mapping).forEach(([id, key]) => {
    const value = $(id).value.trim();
    if (value) answers[key] = value;
  });
  if ($("a-verbal-wage").value) answers.verbal_wage_bdt = Number($("a-verbal-wage").value);
  if ($("a-quoted-fee").value) answers.quoted_fee_bdt = Number($("a-quoted-fee").value);
  if ($("a-cash").checked) answers.cash_payment = true;
  if ($("a-nowritten").checked) answers.written_contract = false;

  const form = new FormData();
  form.append("file", state.file);
  form.append("answers", JSON.stringify(answers));

  try {
    const data = await api("/api/contract", { method: "POST", body: form });
    const x = data.extraction;
    const missing = (x.unclear_or_missing || []).join(", ") || "—";
    box.innerHTML = `
      <h3>চুক্তিতে যা পাওয়া গেছে</h3>
      <div class="summary">${esc(data.summary_bn)}</div>
      <h3 style="margin-top:16px">বিস্তারিত তথ্য</h3>
      <ul class="citations">
        <li>মাসিক বেতন: <span class="sec">${esc(x.monthly_wage && x.monthly_wage.amount != null ? x.monthly_wage.amount : "উল্লেখ নেই")}</span>
            ${esc(x.monthly_wage && x.monthly_wage.currency || "")}</li>
        <li>চুক্তির সময়কাল (মাস): <span class="sec">${esc(x.contract_duration_months ?? "উল্লেখ নেই")}</span></li>
        <li>নিয়োগকর্তা: <span class="sec">${esc(x.employer || "উল্লেখ নেই")}</span></li>
        <li>কাজ: <span class="sec">${esc(x.job_title || "উল্লেখ নেই")}</span></li>
        <li>থাকার ব্যবস্থা: <span class="sec">${x.accommodation_provided === null ? "উল্লেখ নেই" : (x.accommodation_provided ? "লেখা আছে" : "লেখা নেই")}</span></li>
        <li>ফেরার টিকিট: <span class="sec">${x.return_ticket_provided === null ? "উল্লেখ নেই" : (x.return_ticket_provided ? "লেখা আছে" : "লেখা নেই")}</span></li>
        <li>মৃত্যু/আঘাতে ক্ষতিপূরণ: <span class="sec">${x.compensation_for_death_or_injury_stated === null ? "উল্লেখ নেই" : (x.compensation_for_death_or_injury_stated ? "লেখা আছে" : "লেখা নেই")}</span></li>
        <li>ভিসার ধরন: <span class="sec">${esc(x.visa_type || "উল্লেখ নেই")}</span></li>
        <li>যা পাওয়া যায়নি / অস্পষ্ট: <span class="sec">${esc(missing)}</span></li>
      </ul>
      <h3 style="margin-top:16px">সতর্কবার্তা</h3>
      ${flagList(data.flags)}
      <div class="sourceline">পড়ার ধরন: ${esc(data.extraction.source_mode)} · বিশ্লেষণ: ${esc(data.processing_seconds)} সেকেন্ড ·
        মোড: ${esc(data.mode)}${data.injection_removed && data.injection_removed.length ? " · ⚠️ ফাইলের ভেতরে নির্দেশ-ধরনের লেখা উপেক্ষা করা হয়েছে" : ""}</div>
      ${disclaimer("ফাইলটি বিশ্লেষণের পরে সার্ভারে রাখা হয় না।")}
      ${speakButton()}`;
    bindSpeak(box, data.summary_bn);
  } catch (error) {
    box.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
});

/* --------------------------------------------------------------- 3. fee */
$("fee-go").addEventListener("click", async () => {
  const box = $("fee-result");
  box.hidden = false;
  const destination = $("fee-destination").value.trim();
  const amount = Number($("fee-amount").value);
  if (!destination || !amount) {
    box.innerHTML = `<p class="error">দেশের নাম ও টাকার পরিমাণ দুটোই দিতে হবে।</p>`;
    return;
  }
  box.innerHTML = `<p class="muted">তুলনা করা হচ্ছে…</p>`;
  try {
    const data = await api("/api/fee-check", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ destination, quoted_fee_bdt: amount }),
    });
    const cls = data.exceeds_cap === true ? "verdict-notfound"
      : data.exceeds_cap === false ? "verdict-found" : "verdict-unknown";
    box.innerHTML = `
      <h3>ফলাফল</h3>
      <div class="verdict ${cls}">${esc(data.message_bn)}</div>
      ${data.cap ? `<div class="sourceline">সরকার-নির্ধারিত সীমা: ${esc(data.cap.cap_bdt)} টাকা ·
        কার্যকর তারিখ: ${esc(data.cap.effective_date || "অজানা")} ·
        ${data.cap.verified ? "যাচাই করা" : "⚠️ যাচাই করা হয়নি"} · সূত্র: ${esc(data.cap.source)}</div>` : ""}
      ${data.cap && data.cap.note ? `<p class="muted">${esc(data.cap.note)}</p>` : ""}
      ${flagList(data.flags)}
      ${disclaimer()}
      ${speakButton()}`;
    bindSpeak(box, data.message_bn);
  } catch (error) {
    box.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
});

/* --------------------------------------------------------------- 4. ask */
async function ask(question) {
  const box = $("ask-result");
  box.hidden = false;
  if (!question) { box.innerHTML = `<p class="error">প্রশ্নটি লিখুন বা বলুন।</p>`; return; }
  box.innerHTML = `<p class="muted">খোঁজা হচ্ছে…</p>`;
  try {
    const data = await api("/api/ask", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    });
    const citations = (data.citations || []).map((c) => `
      <li><span class="sec">${esc(c.section || c.source_id)}</span> — ${esc(c.title)}
        <div class="muted">${esc(c.snippet)}</div></li>`).join("");
    box.innerHTML = `
      <h3>উত্তর</h3>
      <div class="summary">${esc(data.answer_bn)}</div>
      ${citations ? `<h3 style="margin-top:14px">সূত্র</h3><ul class="citations">${citations}</ul>` : ""}
      <div class="sourceline">${data.grounded ? "সূত্র মিলিয়ে দেওয়া হয়েছে" : "সূত্র যথেষ্ট পাওয়া যায়নি"} ·
        মোড: ${esc(data.mode)}</div>
      ${disclaimer()}
      ${speakButton()}`;
    bindSpeak(box, data.answer_bn);
  } catch (error) {
    box.innerHTML = `<p class="error">${esc(error.message)}</p>`;
  }
}

$("ask-go").addEventListener("click", () => ask($("ask-question").value.trim()));
document.querySelectorAll("[data-ask]").forEach((button) => {
  button.addEventListener("click", () => {
    $("ask-question").value = button.dataset.ask;
    ask(button.dataset.ask);
  });
});

/* --------------------------------------------------------------- start-up */
(async function init() {
  try {
    const [meta, texts] = await Promise.all([api("/api/meta"), api("/api/texts")]);
    state.meta = meta;
    state.texts = texts;

    $("trustline").innerHTML = [
      `এজেন্সি তালিকার তারিখ: <strong>${esc(meta.agency_data_date || "অজানা")}</strong>`,
      meta.agency_data_is_official ? "সরকারি তালিকা" : "⚠️ ডেমো তালিকা",
      meta.mode === "llm" ? "এআই চালু" : "এআই বন্ধ (বেসিক মোড)",
      meta.partner_name_bn ? `অংশীদার: ${esc(meta.partner_name_bn)}` : "",
    ].filter(Boolean).join(" · ");

    const banner = $("demobanner");
    if (!meta.agency_data_is_official) {
      banner.hidden = false;
      banner.textContent = texts.demo_banner_bn;
    }

    $("consent-text").textContent = texts.consent_bn;
    $("helpline-line").textContent = meta.helpline_bn;
    $("helpline-note").textContent = meta.helpline_verified
      ? "অফিসিয়াল হেল্পলাইন"
      : "⚠️ এই নম্বরটি এখনো অফিসিয়াল সূত্রে যাচাই করা হয়নি — লঞ্চের আগে যাচাই করতে হবে। "
        + (texts.passport_note_bn || "");
  } catch (error) {
    $("trustline").textContent = "সার্ভারের সাথে যোগাযোগ করা যাচ্ছে না: " + error.message;
  }
})();
