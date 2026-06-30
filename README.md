# EPISTEME

**A psychiatric clinical decision-support pipeline that is built to fail loudly — surfacing uncertainty, disagreement, and missing evidence instead of projecting false confidence.**

---

## Problem / Inspiration

Clinical AI tools tend to fail in the most dangerous way possible: confidently. A single large language model will produce a fluent, authoritative-sounding psychiatric assessment whether or not the evidence supports it — and a busy clinician has no easy way to see where the model is guessing, where it contradicts the literature, or where it has quietly invented a citation.

EPISTEME is built around the opposite principle: **a decision-support system should make its own uncertainty impossible to miss.** Rather than collapsing everything into one answer, EPISTEME runs multiple independent assessments, stages an adversarial debate between them, grounds claims in retrieved PubMed/OpenFDA evidence, and refuses to proceed when the evidence is insufficient. It is designed for Thai-language psychiatric intake, with a privacy gate that keeps patient text on the local machine.

This is a hackathon prototype, and the sections below are deliberately honest about the gap between the designed architecture and what the current prototype actually runs.

---

## Architecture

### (a) Designed architecture

The intended design is a **three-model assessment followed by an independent adversarial debate**, with evidence grounding and a hard privacy gate:

1. **Input processing** — a Thai patient presentation enters via a chat trigger and is normalized.
2. **Privacy gate (local-only)** — a local Ollama model plus a PyThaiNLP NER service (`privacy_ner_service.py`, runs at `127.0.0.1:5005`) and regex scanning detect personally identifying information. A generalization rewriter strips or generalizes PII, and a re-review step confirms the text is clean **before any data is sent to a cloud model**. If the gate cannot clear the text, the pipeline stops.
3. **Evidence gathering** — PubMed E-utilities (`esearch` + `efetch`) retrieve abstracts, and OpenFDA supplies drug-safety labels. Evidence is bundled and attached to the case.
4. **Three independent assessments** — the case is assessed in parallel by **three distinct models** (designed as Gemini Pro + GPT-5.5 + Opus 4.8), each producing a differential diagnosis, severity estimate, and evidence-cited recommendation.
5. **Verification + quantum scoring** — a verification model checks each assessment for unsupported claims; a "Quantum Evidence Selector" and "Quantum Score Calculator" score the candidate assessments across weighted dimensions (ethics, research backing, accuracy, completeness, confidence).
6. **Independent debate** — separate debater models (designed as Groq Llama + OpenRouter DeepSeek) challenge the leading assessment: flagging claims unsupported by the retrieved PMIDs, missing DSM-5 criteria, ethical concerns, and logical flaws, then generating clinical follow-up questions. The debaters are intentionally **different models from the assessors** to avoid self-agreement.
7. **Synthesis + clinical summary** — the debate is synthesized, and a Thai-language clinical summary is produced (designed via Typhoon), with explicit guardrails against prescribing or stating drug dosages.
8. **Insufficient-evidence path** — if evidence/agreement is too weak, the pipeline returns an explicit "insufficient evidence" result rather than a fabricated answer.

A separate **Batch Evaluation Mode** benchmarks the full EPISTEME pipeline against GPT-5.5 and Opus 4.8 standalone over a 100-case set, scored by rule-based metrics plus an **independent LLM judge (Llama-3.1-70B via OpenRouter)** that did not generate any of the evaluated outputs.

### (b) Current prototype configuration

Being honest here, because this project's entire pitch is "fail loudly, don't pretend to be confident" — hiding this would undercut the submission:

- **Most of the cloud-AI assessment/debate slots are currently backed by Gemini**, not by the three/four distinct models the design calls for. This is a prototype/time-constraint decision made to get an end-to-end pipeline working within the hackathon window (you will see a "Verification Gemini Shim" and Gemini standing in for several nominal slots in the live workflow).
- As a result, the "independent debate" and "three independent assessments" currently have **less genuine model diversity than the design intends**. When the same base model plays multiple roles, the adversarial value of the debate is reduced.
- A production version would need **real model diversity** across the assessment and debate roles (genuinely different model families) and/or **fine-tuning** for the psychiatric/Thai-clinical domain before any of the comparative claims should be trusted.
- The **privacy gate and verification still run on a local Ollama model** (DeepSeek-R1), keeping patient text local as designed — this part is not shimmed.

---

## Known current limitations

- **Quantum score is mis-calibrated for clean single assessments.** The "Quantum Score Calculator" formula appears tuned for richer, more divergent differentials than a single clean assessment naturally produces. In testing, **verified-good data still scored only 54/85** — i.e. an assessment that was independently confirmed as clinically sound was penalized by the scoring formula. The score is therefore not yet trustworthy as an absolute quality gate; it currently behaves more like a relative signal that expects high inter-assessment divergence.
- **Limited model diversity in the prototype** (see *Current prototype configuration* above) — the headline 3-model + independent-debate design is only partially realized.
- **No clinical validation.** EPISTEME is a research/demo prototype. It is **not** a medical device, has not been clinically validated, and must not be used for real patient care.
- **Benchmark scale.** The batch harness is built for a 100-case set; results at this scale are indicative, not statistically conclusive.
- **Thai-language focus.** The pipeline (privacy NER, clinical summary) is tuned for Thai input; other languages are untested.

---

## Setup

> EPISTEME is a multi-component pipeline (n8n + local Python service + Ollama + several cloud APIs). It is **not** a one-click app; the steps below take it from clone to a runnable local pipeline.

### Prerequisites

- **Python 3.10+** (for the PyThaiNLP privacy NER service and helper scripts)
- **Node.js 18+** (required by n8n)
- **n8n** (self-hosted) — `npm install -g n8n`
- **Ollama** with a local model pulled (e.g. `ollama pull deepseek-r1:8b`)
- **API keys** for the cloud models you intend to enable — see [`.env.example`](.env.example):
  Gemini, OpenRouter, Groq, Typhoon, and optionally an NCBI/PubMed key.

### Steps

1. **Clone**
   ```bash
   git clone <this-repo-url>
   cd EPISTEME
   ```

2. **Configure secrets**
   ```bash
   cp .env.example .env
   # edit .env and fill in your own keys
   ```
   In the n8n UI, create matching **Credentials** (Settings → Credentials) for Gemini, Groq, OpenRouter, and Typhoon. The workflow nodes reference these by credential, not by hardcoded keys.

3. **Start the privacy NER service**
   ```bash
   pip install -r requirements.txt   # or: pip install flask pythainlp
   python privacy_ner_service.py     # serves on http://127.0.0.1:5005
   ```

4. **Start Ollama**
   ```bash
   ollama serve
   ollama pull deepseek-r1:8b
   ```

5. **Start n8n and import the workflows**
   ```bash
   n8n
   # open http://localhost:5678
   ```
   Import the two workflow files from [`n8n/`](n8n/):
   - `EPISTEME_Clinical_Decision_Support.json` — the interactive clinical pipeline
   - `EPISTEME_Batch_Evaluation.json` — the 100-case benchmark
   The PubMed node's `api_key` field ships as the placeholder `YOUR_NCBI_API_KEY`; leave it blank to use the keyless rate limit, or set your own key.

6. **Open a dashboard**
   - `episteme_dashboard.html` — interactive clinical mode (talks to the n8n chat webhook on `localhost:5678` and the NER service on `127.0.0.1:5005`)
   - `episteme_batch.html` — batch evaluation view

> The 100-case benchmark data (`100_cases.json`) is **not** included in this repo. It is derived from the PDCH dataset (Cao et al. 2025, *Scientific Data*; CC BY-NC-ND), which is not redistributable — obtain it from the original source if you wish to reproduce the benchmark.

---

## Tech stack

- **Orchestration:** n8n (self-hosted, LangChain nodes)
- **Privacy / local inference:** Ollama (DeepSeek-R1), PyThaiNLP NER service (Python / Flask)
- **Cloud models:** Google Gemini, OpenRouter (GPT-5.5, Opus 4.8, Llama-3.1-70B), Groq (DeepSeek-R1), Typhoon (Thai)
- **Evidence sources:** PubMed E-utilities (NCBI), OpenFDA drug labels
- **Frontend:** vanilla HTML/CSS/JS dashboards
- **Benchmark output:** Google Sheets (batch results)
- **Language:** Python, JavaScript

---

## Demo video

📹 _Coming soon — link to be added after recording._

---

## License

All rights reserved. This repository is made publicly viewable **only** for hackathon/competition evaluation and demonstration. See [LICENSE](LICENSE) for the full terms. Any use beyond evaluation requires explicit written permission from the copyright holders.

---

## Acknowledgments

- **PubMed / NCBI E-utilities** — biomedical literature retrieval.
- **OpenFDA** — drug-label and safety data.
- **PyThaiNLP** — Thai-language NLP / NER used in the privacy gate.
- **Ollama** and the open-weight model community (DeepSeek-R1, Llama) — local and independent inference.
- **Typhoon (SCB 10X)** — Thai-language LLM.
- The **PDCH dataset** authors (Cao et al. 2025, *Scientific Data*) for the benchmark source data.
- Mentors, reviewers, and research collaborators who supported the EPISTEME team during development.
