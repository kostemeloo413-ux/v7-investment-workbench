# Blogger Lab (博主实验室) Architecture & Technical Specification

## Overview

**Blogger Lab** is an experimental research module integrated into the V7 Investment Workbench. It provides a semi-automated pipeline for translating unstructured investment research notes and strategy ideas into deterministic, verifiable trading rules:

1. **Ingestion & Text Slicing**: Normalizes research texts into immutable chunked batches.
2. **Knowledge Extraction**: Extracts quantitative entry/exit concepts and anchors them to exact verbatim citation slices.
3. **Strategy DSL & Human Confirmation**: Compiles extracted knowledge into a strongly-typed Strategy DSL with rigid risk controls (stop loss, take profit, position limits), requiring human review before strategy publication.
4. **Opportunity Scanning**: Evaluates the mainboard stock universe against published strategy conditions on historical or current market sessions.
5. **Controlled Paper Replay**: Simulates order execution under strict A-share trading mechanics (T+1 holding locks, stamp taxes, commissions, and slippage) inside an isolated SQLite ledger.

---

## Architectural Principles & Isolation Boundaries

### 1. Default-Off Operational Gate
Blogger Lab is disabled by default in public distributions:
```ini
# .env
V7_BLOGGER_LAB_ENABLED=0
```
When disabled:
- The FastAPI application factory does not import or mount `/api/blogger-lab/*` endpoints.
- No background workers or storage locks are initialized.
- The V7 workspace displays honest empty/disabled indicators if accessed.

### 2. Dedicated Storage Root
To enable Blogger Lab, an explicit and isolated directory path must be provided:
```ini
V7_BLOGGER_LAB_ENABLED=1
V7_BLOGGER_LAB_ROOT=./data/blogger_lab
```
The module maintains complete separation from formal production ledgers:
- Research artifacts and extraction metadata are saved in `lab.sqlite`.
- Paper trading positions and fills are recorded in `lab_paper.sqlite`.
- It never modifies the market database (`market.db`) or core trading ledgers (`data/trades/*.json`).

### 3. Local Loopback Security Boundary
All `/api/blogger-lab/*` routes enforce strict local access controls:
- **Proxy Rejection**: Explicitly rejects `X-Forwarded-For`, `Forwarded`, and `X-Real-IP` headers with HTTP 403 Forbidden.
- **Physical Socket Validation**: Restricts client sockets to loopback addresses (`127.0.0.1`, `::1`).
- **Same-Origin Check**: Cross-origin requests, `Origin: null`, and non-matching origins are blocked.

---

## Quickstart & Verification

### Running the Public Preview with Blogger Lab Enabled

The actual public preview entry point is `python -m scripts.public_preview` (factory: `scripts.public_preview:create_preview_app`), serving on `http://127.0.0.1:8020` by default.
The preview server loads configuration from environment variables or a local `.env` file (copied from `.env.example`).

#### 1. Executable Enablement Commands

**Windows PowerShell**:
```powershell
$env:V7_BLOGGER_LAB_ENABLED="1"
$env:V7_BLOGGER_LAB_ROOT="./data/blogger_lab"
python -m scripts.public_preview --port 8020
```

**Windows Command Prompt (cmd.exe)**:
```cmd
set V7_BLOGGER_LAB_ENABLED=1
set V7_BLOGGER_LAB_ROOT=./data/blogger_lab
python -m scripts.public_preview --port 8020
```

**Linux / macOS (Bash / Zsh)**:
```bash
V7_BLOGGER_LAB_ENABLED=1 V7_BLOGGER_LAB_ROOT=./data/blogger_lab python -m scripts.public_preview --port 8020
```

#### 2. Launch via `.env` File
Copy `.env.example` to `.env` in the repository root and set:
```ini
V7_BLOGGER_LAB_ENABLED=1
V7_BLOGGER_LAB_ROOT=./data/blogger_lab
```
Then start the preview server:
```bash
python -m scripts.public_preview --port 8020
```
Navigate to `http://127.0.0.1:8020/v7#page-blogger-lab` or `http://127.0.0.1:8020/demo`.

#### 3. Honest Missing Dependencies & Boundary Disclosure

- **LLM API Credentials**: Without configuring `DEEPSEEK_API_KEY` or `LLM_API_KEY` in `.env`, calling the live extraction endpoint will report an honest configuration error. Persona registration, corpus batch upload, text slicing, and DSL strategy formulation work completely offline.
- **Market Data**: The public release distribution does not bundle private production market databases (`data/market/market.db`). Live universe scans will honestly indicate missing market evidence. Offline mock scenarios (such as `standard_t1_cycle`) are provided to test the deterministic T+1 order matching and fee accounting offline.
- **Preview Isolation Boundary**: The public preview server does not start trading daemons, background schedulers, or watchdogs. All general preview write endpoints (`/api/run/*`) remain strictly 403 Forbidden (`READ_ONLY_PREVIEW`). Only `/api/blogger-lab/*` write operations are allowed locally, protected by loopback socket and same-origin validation.

---

### Synthetic Test Sample & Automated Tests

A synthetic, fictional sample note is provided for verification:
```
examples/blogger_lab/01_synthetic_researcher_sample.txt
```

Run the unit test suite:
```bash
python -B -m pytest -q tests/test_blogger_lab.py
```
