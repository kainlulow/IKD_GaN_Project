# Deterministic Literature Intelligence (V1)  
### A Reproducible Literature Curation Framework for GaN CMOS Research

---

## 1. Overview

This repository implements **V1 of a Deterministic Literature Intelligence (DLI)** framework, designed to systematically discover, validate, and structure literature for **GaN CMOS and complementary logic research**.

The goal of V1 is **not** to summarize or interpret papers, but to construct a **trusted, continuously updated, and auditable literature substrate** that can support:

- comprehensive review papers  
- gap identification  
- TCAD / DOE design justification  
- downstream AI- or LLM-assisted analysis (V2+)  

> **V1 is fully deterministic and does not rely on LLM reasoning.**

---

## 2. Design Philosophy

### Core principles

- **Deterministic**  
  All decisions are rule-based and reproducible.

- **Auditable**  
  Every inclusion, exclusion, and update is traceable via logs and version history.

- **Separation of concerns**  
  - V1: discovery, validation, structuring  
  - V2+: interpretation, summarization, reasoning

- **Human-in-the-loop by design**  
  Ambiguous cases are explicitly routed for manual review rather than guessed.

---

## 3. What V1 Does (and Does Not Do)

### ✅ What V1 does

- Periodically searches the global literature via **Crossref DOI metadata**
- Applies **compound-intent queries** tailored to GaN CMOS logic
- Verifies DOI / URL existence
- Deduplicates records deterministically
- Applies **rule-based keyword tagging**
- Routes papers into:
  - a trusted **Literature Master**
  - an uncertainty **Review Queue**
- Maintains a complete **audit log**
- Runs automatically via **GitHub Actions**

### ❌ What V1 does NOT do

- Read full PDFs
- Summarize papers
- Judge novelty or importance
- Rank papers by quality
- Use LLMs or machine learning

These capabilities are intentionally deferred to **V2**.

---

## 4. Repository Structure
```
.
├── 00_taxonomy/
│ └── IKD_Taxonomy.yaml # Rule-based keyword taxonomy
│
├── 01_literature/
│ ├── IKD_Literature_Master.xlsx # Curated literature substrate
│ ├── IKD_ReviewQueue.xlsx # Ambiguous entries for human review
│ └── IKD_RunLog.csv # Full audit trail of all runs
│
├── 02_reports/
│ └── (reserved for downstream analysis / V2 outputs)
│
├── 03_queries/
│ └── queries.json # Declarative search intent & policy
│
├── literature_agent_v1/
│ ├── run_incremental.py # Main deterministic execution script
│ ├── requirements.txt
│ └── state/
│ └── last_run_timestamp.txt # Incremental search checkpoint
│
├── .github/
│ └── workflows/
│ └── ikd_gan_cmos_daily.yml # GitHub Actions scheduler
│
└── README.md
```

## 5. Deterministic Literature Intelligence (V1) Pipeline

The V1 pipeline consists of the following stages:

1. **Deterministic Discovery**
   - Query Crossref using keyword-based compound-intent queries
   - Queries are defined in `queries.json` (Strategy A & B)

2. **Deterministic Validation**
   - Require DOI or URL
   - Drop duplicate DOI or near-identical titles

3. **Rule-based Structuring**
   - Apply keyword rules from `IKD_Taxonomy.yaml`
   - Tag each paper by:
     - Device type
     - Method (e.g., TCAD, experiment)
     - Enabler category

4. **Deterministic Routing**
   - High-confidence entries → `IKD_Literature_Master.xlsx`
   - Ambiguous entries → `IKD_ReviewQueue.xlsx`

5. **Audit Logging**
   - Every run is recorded in `IKD_RunLog.csv`
   - GitHub commit history provides temporal provenance

---

## 6. Key Files Explained

### `queries.json`
- Defines **where the agent searches**
- Contains:
  - compound-intent query strings (active)
  - declarative filtering and policy rules (documented, not all enforced in V1)
- Only the `"buckets"` section is actively used in V1

### `IKD_Taxonomy.yaml`
- Defines **how papers are tagged**
- Keyword-based, deterministic, and human-editable
- No semantic inference or learning

### `IKD_Literature_Master.xlsx`
- The trusted literature substrate
- Safe to use for:
  - counting
  - trend analysis
  - review tables
  - downstream AI input

### `IKD_ReviewQueue.xlsx`
- Buffer for uncertain or ambiguous cases
- Requires human judgment
- Prevents pollution of the Master table

### `run_incremental.py`
- The execution engine
- Implements:
  - search iteration
  - validation
  - deduplication
  - rule-based tagging
  - routing
- Contains **no LLM logic**

---

## 7. Automation via GitHub Actions

V1 runs automatically using GitHub Actions:

- **Trigger**: scheduled (e.g., daily or weekly) or manual
- **Execution**:
  - installs dependencies
  - runs `run_incremental.py`
  - commits updated results back to the repository
- **Benefits**:
  - zero infrastructure maintenance
  - reproducibility
  - full audit trail

---

## 8. Intended Usage Pattern

### Daily
- No manual action required

### Weekly
- Review `IKD_ReviewQueue.xlsx`
- Promote relevant entries to Master if appropriate

### Periodically
- Adjust query wording or taxonomy keywords if systematic gaps are observed

---

## 9. Relationship to V2 and Beyond

V1 provides the **deterministic foundation** for later stages:

- **V2**: LLM-assisted reading and summarization  
  - Reads *only* from `IKD_Literature_Master.xlsx`
  - Uses abstracts or selectively full text
- **V3+**: physics–AI–DOE integration

> **V1 constrains the problem space; V2 interprets within it.**

---

## 10. Status and Scope

- V1 is considered **complete when**:
  - coverage is broad but controlled
  - Master table grows predictably
  - ambiguity is handled via ReviewQueue
  - results are trusted for review writing

At that point, V1 becomes **research infrastructure**, not an active experiment.

---

## 11. License and Disclaimer

This repository is intended for **research and academic use**.

- Metadata is sourced from public DOI registries
- No copyrighted full-text content is redistributed
- Users are responsible for complying with publisher access policies

---

## 12. Citation (suggested)

If you build upon this framework, please cite it conceptually as:

> *A deterministic literature intelligence framework for systematic, reproducible, and continuously updated literature curation.*

---

### Final note

This repository is intentionally **boring, conservative, and transparent**.  
That is a feature — not a limitation.
