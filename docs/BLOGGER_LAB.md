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

A synthetic, fictional sample note is provided for verification:
```
examples/blogger_lab/01_synthetic_researcher_sample.txt
```

Run the unit test suite:
```bash
python -B -m pytest -q tests/test_blogger_lab.py
```
