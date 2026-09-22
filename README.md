# প্রবাসী সুরক্ষা — Migrant Worker Fraud Shield (working prototype)

A Bangla-first tool that lets an aspiring migrant worker check a recruiter, understand a
contract, spot red flags and know where to complain — **before paying**.

This repository is the working prototype of the v1 plan (draft 21 Sept 2026). It runs
end-to-end today: four modules, a Bangla mobile UI with voice input/output, a labelled
test set, an evaluation harness, and 99 passing tests.

```
┌──────────────┐   ┌───────────────────────┐   ┌──────────────────────────────┐
│ Worker's     │   │ Bangla web app        │   │ FastAPI backend              │
│ phone        │──▶│ (4 tabs, voice,       │──▶│ licence │ contracts │ rules   │
│ (slow link)  │   │  trust bar, helpline) │   │ rag     │ safety    │ llm     │
└──────────────┘   └───────────────────────┘   └──────────────────────────────┘
                                                  │            │           │
                                    agencies.csv  │  fee_caps  │  act_chunks
                                    (dated)       │  (dated)   │  + rule_cards
```

---

## 1. Quickstart

```bash
cd fraud-shield
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt

# 1. Download the Act and build the section index (one-off)
curl -sSL -o backend/data/raw/act2013.pdf \
  'http://asianparliamentarians.mfasia.org/wp-content/uploads/2017/01/bangladesh_overseas_empl_migrants_act2013-_eng.pdf'
python - <<'PY'
from pypdf import PdfReader
r = PdfReader("backend/data/raw/act2013.pdf")
open("backend/data/raw/act2013.txt","w").write("\n\n===PAGE===\n\n".join((p.extract_text() or "") for p in r.pages))
PY
python scripts/build_index.py          # -> backend/data/act_chunks/section_*.json (49 sections)

# 2. Run
cp .env.example .env                   # optional: add ANTHROPIC_API_KEY for AI mode
PYTHONPATH=backend .venv/bin/uvicorn app.main:app --reload --port 8000
# open http://localhost:8000

# 3. Verify
cd backend && ../.venv/bin/python -m pytest tests -q       # 99 tests
cd .. && .venv/bin/python eval/make_test_set.py            # build the labelled test set
.venv/bin/python eval/run_eval.py --json eval/report.json  # score every module
```

**Runs with no API key.** Without `ANTHROPIC_API_KEY` the app is in *offline mode*: the
licence checker, fee checker and rule engine are fully functional (they never needed AI),
contract reading falls back to the deterministic regex reader, and `/ask` returns quoted
law with citations instead of generated Bangla prose.

---

## 2. What is real and what is a placeholder

This distinction is the most important thing on this page.

| Item | State in this prototype |
|---|---|
| Overseas Employment and Migrants Act 2013 text (49 sections, indexed, citable) | **Real** — extracted from the official English translation, `verified: false` until compared with the Legislative Division copy |
| Rule cards (fees, contract clauses, complaints, penalties) | **Real drafting**, awaiting reviewer sign-off; every line carries a source |
| Red-flag rule engine | **Real** — 10 rules, each with its legal basis, unit-tested |
| Fee ceilings (Saudi Arabia Tk 165,000 / Malaysia Tk 78,990) | **Unverified** — press-reported, `verified: false`, effective dates unknown; the UI shows a warning and the engine raises a data-quality flag |
| Agency list (`backend/data/agencies.csv`, 40 rows) | **Synthetic demo data.** Every row is marked `SYNTHETIC`; a red banner appears on every licence answer and `AGENCY_DATA_IS_OFFICIAL=false` |
| Helpline number (16135) | **Press-reported (2022)**, `HELPLINE_VERIFIED=false` → the UI says it is not yet verified |
| Institutional partner name/logo | **Empty by design** — the app never invents a partner to look trustworthy |
| Voice input/output | **Real**, via browser Web Speech API (`bn-BD`) — quality depends on the device voice; documented as an evaluation item, not a solved problem |

---

## 3. The four modules

| Module | Endpoint | AI used? |
|---|---|---|
| Licence checker — name or RL number → found / not found + data date | `POST /api/licence` | No — local CSV lookup, exact RL first then fuzzy name |
| Contract explainer — photo/PDF → plain-Bangla summary | `POST /api/contract` | Yes for reading; Bangla summary is assembled from validated JSON |
| Red-flag detector — rule engine + optional model pass | `POST /api/contract`, `POST /api/fee-check` | Partly — the model may add flags, never remove them |
| Complaint/rights guide — cited answers | `POST /api/ask` | Yes (RAG over the Act + rule cards) |

Plus `GET /health`, `GET /api/meta` (data dates, helpline, mode) and `GET /api/texts`
(all fixed Bangla copy in one place, so the UI cannot invent wording).

### Safety invariants — enforced in code, not just in prompts

1. **Never "safe".** `safety/guard.py:unsafe_verdict_hits` scans every user-facing string.
   The tool says *found in the list dated X* or *not found*, plus "confirm with BMET".
   A regression test asserts the wording; a false reassurance is the worst failure this
   product can have.
2. **No guessing.** `null` means "not stated". An unreadable image raises a friendly
   "take a clearer photo" error instead of returning an empty analysis that reads as
   "no problems found".
3. **Documents are data, not instructions.** `neutralise_untrusted` strips
   instruction-looking lines before the text reaches a prompt, and the model is told to
   ignore them; a model-supplied "unusual clause" is only accepted if that exact phrase
   really appears in the document.
4. **Grounded or silent.** An `/ask` answer with no citation, or with a number that is
   not in the retrieved sources, is discarded and replaced by the refusal text.
   Weak retrieval → "I could not find this in the official information I have."
5. **Nothing is stored.** The upload is processed from memory and never written to disk —
   structurally true, not a cleanup job that might not run. `test_no_file_is_written_to_disk`
   checks it.
6. **PII out of logs.** `redact_pii` removes passports, NIDs, phones and long digit runs
   from anything logged or echoed (wage figures deliberately survive, because the tool has
   to explain them).
7. **No blacklists.** Only official/licensed data is used; no user-report shaming
   (defamation risk).

---

## 4. Evaluation (plan §9 test set)

`eval/run_eval.py` scores every module against the plan's targets. Latest run, offline
mode (no API key), on 30 licence queries + 20 mock contracts + 40 Q&A pairs:

```
metric                                                  target      actual
Exact RL numbers correct                                100.0%      100.0%  PASS
Typo'd / partial names correct (top-1)                   90.0%       90.0%  PASS
Typo'd names: right agency offered as a candidate       100.0%      100.0%  PASS
Invalid numbers rejected (not mis-matched)              100.0%      100.0%  PASS
Contract key fields correct                              90.0%       93.2%  PASS
Planted red flags detected                               85.0%      100.0%  PASS
False alarms on clean contracts (lower is better)        15.0%        0.0%  PASS
Q&A: right section in top 3                              80.0%       85.0%  PASS
Q&A answers with no citation (must be 0)                  0.0%        0.0%  PASS
Contract analysis P95 (seconds, LLM path target 20s)    20.00s       0.00s  PASS
```

Honest reading of those numbers:

* The licence and Q&A figures are meaningful immediately — they run the same code path a
  user would hit.
* **Contract fields and flags are measured through the regex reader**, because there is no
  API key in this environment. The 93.2% field accuracy and 100% flag recall are therefore
  a floor, not a claim about the AI path. Re-run with `ANTHROPIC_API_KEY` set to measure
  the model; the run prints which mode produced the numbers.
* Fields the regex reader cannot read (the Arabic-only fixture) are reported as *skipped*,
  not counted as passes and not quietly failed.
* `qa_uncited` counts only *answered* questions — a refusal makes no claim, so it cannot
  be an uncited claim. Refusals are reported separately; 4/40 were refused, which is a
  coverage signal, not something to fix by relaxing the grounding rule.
* The one typo'd-name "failure" (`Arial Kha`) returns **ambiguous** with both Arial Khan
  agencies listed — the intended behaviour, since two real candidates exist.

---

## 5. Layout

```
fraud-shield/
├── backend/
│   ├── app/
│   │   ├── main.py              # routes, CORS, rate limits, static frontend
│   │   ├── config.py            # every setting from env; no secrets in code
│   │   ├── schemas.py           # typed boundaries; the LLM only fills ContractExtraction
│   │   ├── errors.py            # typed errors + JSON logging with PII redaction
│   │   ├── llm.py               # Anthropic Messages client (timeout, retry, backoff)
│   │   ├── licence/search.py    # exact RL lookup, then fuzzy name matching
│   │   ├── rules/engine.py      # fee caps + all red-flag rules
│   │   ├── contracts/
│   │   │   ├── docio.py         # sniff, validate, PDF text + self-checking repair
│   │   │   ├── extract.py       # LLM extraction + deterministic fallback
│   │   │   ├── summarize.py     # Bangla summary from validated JSON + flags
│   │   │   └── pipeline.py      # ordering of the whole analysis (HTTP-free, testable)
│   │   ├── rag/
│   │   │   ├── index.py         # BM25 + Bangla keywords; embedding seam documented
│   │   │   └── answer.py        # citation-mandatory answering, refusal behaviour
│   │   └── safety/guard.py      # disclaimers, redaction, injection, "never safe"
│   ├── data/                    # agencies.csv, fee_caps.json, rule_cards.md, act_chunks/
│   └── tests/                   # 99 tests across safety, licence, rules, rag, contracts, api
├── frontend/                    # index.html + app.js + styles.css (Bangla UI, voice)
├── eval/                        # make_test_set.py, contracts/, qa_pairs.json, run_eval.py
├── scripts/                     # build_index.py, refresh_agencies.py
└── .env.example
```

### Design decisions worth knowing

**Why BM25 and not embeddings.** The corpus is ~50 sections; BM25 is deterministic, free
and auditable, which matters when answers carry legal citations. The real problem is
cross-lingual: the Act is English, users ask in Bangla. That is handled for now by curated
Bangla keywords per section (`scripts/build_index.py`) plus bilingual rule cards, with
suffix-aware stemming. `rag/index.py:EmbeddingRetriever` is the seam where the plan's BGE-M3
/ multilingual-E5 upgrade drops in — deliberately unimplemented, because an unvalidated
swap would weaken the citation guarantee. **Bangla retrieval is mitigated, not solved.**

**Why the Bangla summary is template-generated by default.** The plan requires the
explanation to come from validated JSON + flags, not the raw document. The template is the
primary implementation; the model may only rewrite it, and a rewrite that loses or invents
a number is discarded and the template is used.

**Why two extraction paths.** The regex reader is not a fallback for show — it keeps the
product working when the model is down, and it cross-checks that the model has not invented
fields. It also reads Bangla numerals (১৩০০) and Bangla keywords.

---

## 6. Deployment (plan §10)

```bash
# backend: Render / Railway / Fly.io
PYTHONPATH=backend uvicorn app.main:app --host 0.0.0.0 --port $PORT

# frontend is served by the same app (same origin, no CORS pain in the pilot);
# it can also be deployed separately - set ALLOWED_ORIGINS to the frontend URL.
```

Checklist before a public pilot:

- [ ] Set `ANTHROPIC_API_KEY` in the host dashboard (never in the repo), pick the model in
      `ANTHROPIC_MODEL`, set a monthly spend cap and alerts in the Anthropic console.
- [ ] Replace `backend/data/agencies.csv` with the real BMET list via
      `scripts/refresh_agencies.py`, spot-check 20 rows, then set
      `AGENCY_DATA_IS_OFFICIAL=true`. The script refuses to overwrite when it parses fewer
      than half the existing rows, because a truncated list would produce false "not found"
      answers.
- [ ] Verify the fee ceilings against the latest circular; set `effective_date` and
      `verified: true` for each.
- [ ] Confirm the helpline is live, then set `HELPLINE_VERIFIED=true`.
- [ ] Add the paying party's/partner's name and logo only after it is real
      (`PARTNER_NAME_BN`, `PARTNER_LOGO_URL`).
- [ ] Rate limiting is in-process (`main.py`); move to a shared store before running more
      than one instance.
- [ ] Weekly job to refresh the agency list and alert on source-format changes.

### `[verify]` list carried over from the plan

Latest BMET/Ministry fee circulars (SA + MY) · whether a BMET/RAIMS download or API exists
and its terms of use · current complaint intake channel and document checklist ·
the 2017 Rules and any 2023 amendment (contract clauses, sub-agent provisions) · that 16135
is still active · current free-tier limits on the chosen hosts · current model IDs and
pricing.

---

## 7. Known limitations

* **Bangla speech quality varies by device.** The UI degrades to typing when the browser has
  no Bangla voice; there is no server-side STT/TTS yet (the plan's "evaluate before
  committing" item is still open).
* **Images need the AI path.** With no API key, a photo cannot be read — the tool says so
  instead of guessing. With a key, images and PDFs go to the model's vision/document input.
* **Arabic-only contracts are read by the model only**; the regex reader has no Arabic
  keyword table.
* **Fee ceilings cover Saudi Arabia and Malaysia only**, and both are unverified.
* **Retrieval is lexical.** Bangla paraphrases that miss the curated keywords fall through
  to the refusal path.
* **Rate limiting and caching are in-process** — fine for one pilot instance, not for scale.
* The prototype does not implement saved case history, an NGO dashboard, offline PWA,
  Telegram/WhatsApp channels, or complaint submission. Those remain post-MVP per the plan.

---

## 8. Sources

* Overseas Employment and Migrants Act 2013 (official English translation) —
  <http://asianparliamentarians.mfasia.org/wp-content/uploads/2017/01/bangladesh_overseas_empl_migrants_act2013-_eng.pdf>;
  authoritative copy: Legislative Division portal (S12 in the plan).
* Rule cards cite individual sections inline.
* Fee ceilings: press reports S7 (The Business Standard, Jul 2022) and S8 (New Age) — marked
  unverified in `fee_caps.json`.
* Helpline: The Business Standard, 5 Sept 2022 —
  <https://www.tbsnews.net/bangladesh/migration/hotline-launched-migrant-workers-assistance-490534>
* Model IDs: Anthropic model overview (checked at build time; re-check before launch).
* All other press sources are listed as S1–S13 in the project plan.

Nothing in this repository is legal advice, and the tool is built so that it cannot pretend
to be.
