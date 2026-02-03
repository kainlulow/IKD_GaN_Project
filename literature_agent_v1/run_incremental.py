#!/usr/bin/env python3
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
QUERIES_PATH = ROOT / "03_queries" / "queries.json"
TAXONOMY_PATH = ROOT / "00_taxonomy" / "IKD_Taxonomy.yaml"

LIT_DIR = ROOT / "01_literature"
REPORT_DIR = ROOT / "02_reports"
STATE_DIR = ROOT / "literature_agent_v1" / "state"

MASTER_XLSX = LIT_DIR / "IKD_Literature_Master.xlsx"
QUEUE_XLSX = LIT_DIR / "IKD_ReviewQueue.xlsx"
RUNLOG_CSV = LIT_DIR / "IKD_RunLog.csv"

LAST_TS_PATH = STATE_DIR / "last_run_timestamp.txt"

CROSSREF_API = "https://api.crossref.org/works"
OPENALEX_API = "https://api.openalex.org/works"

DEFAULT_YEAR_MIN = 2010
CROSSREF_ROWS_PER_QUERY = 200  # default, override via queries.json meta.crossref.rows_per_page
REQUEST_TIMEOUT = 30

# -----------------------------
# Utilities
# -----------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def normalize_title(t: str) -> str:
    t = (t or "").strip().lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 \-:]", "", t)
    return t

def safe_join(parts: List[str], sep: str = " | ") -> str:
    parts = [p.strip() for p in parts if isinstance(p, str) and p.strip()]
    return sep.join(parts)

def load_json(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_yaml(path: Path) -> Dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def ensure_dirs():
    for d in [LIT_DIR, REPORT_DIR, STATE_DIR]:
        d.mkdir(parents=True, exist_ok=True)

def _strip_jats(text: str) -> str:
    text = text or ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# -----------------------------
# Crossref Querying (Cursor paging)
# -----------------------------
def crossref_search_cursor(query: str, year_min: int, rows: int, max_items: int) -> List[Dict]:
    """
    Cursor-based pagination for Crossref to avoid missing results beyond first page.
    Deterministic caps: rows per page + max_items per query.
    """
    cursor = "*"
    out: List[Dict] = []
    fetched = 0

    while True:
        params = {
            "query": query,
            "filter": f"from-pub-date:{year_min}-01-01",
            "rows": rows,
            "cursor": cursor,
        }
        r = requests.get(CROSSREF_API, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        msg = r.json().get("message", {}) or {}
        items = msg.get("items", []) or []
        next_cursor = msg.get("next-cursor", None)

        if not items:
            break

        out.extend(items)
        fetched += len(items)

        if fetched >= max_items:
            break

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    return out

def extract_record(item: Dict, bucket_id: str, query: str) -> Dict:
    title = (item.get("title") or [""])[0]
    doi = (item.get("DOI") or "").strip()
    url = (item.get("URL") or "").strip()
    issued = item.get("issued", {}).get("date-parts", [[]])
    year = issued[0][0] if issued and issued[0] else None

    container = ""
    if item.get("container-title"):
        container = item["container-title"][0]

    authors = []
    for a in item.get("author", []) or []:
        given = a.get("given", "")
        family = a.get("family", "")
        name = (given + " " + family).strip()
        if name:
            authors.append(name)

    abstract = _strip_jats(item.get("abstract", "") or "")

    return {
        "Title": title,
        "Year": int(year) if isinstance(year, int) else "",
        "Venue": container,
        "DOI": doi,
        "URL": url,
        "Authors": "; ".join(authors),
        "Abstract": abstract,
        "BucketID": bucket_id,
        "Query": query,
        "RetrievedAtUTC": utc_now_iso()
    }

# -----------------------------
# OpenAlex (Citation expansion)
# -----------------------------
def _oa_polite_sleep(cfg: Dict):
    delay = float(cfg.get("meta", {}).get("openalex", {}).get("polite_delay_sec", 0.2))
    time.sleep(delay)

def openalex_get_by_doi(doi: str) -> Optional[Dict]:
    """
    OpenAlex DOI lookup. Returns first matching work or None.
    """
    doi = (doi or "").strip().lower()
    if not doi:
        return None
    params = {"filter": f"doi:https://doi.org/{doi}"}
    r = requests.get(OPENALEX_API, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    res = r.json().get("results", []) or []
    return res[0] if res else None

def openalex_fetch_work_by_id(work_id_url: str) -> Optional[Dict]:
    """
    work_id_url is typically like https://openalex.org/W...
    """
    if not work_id_url:
        return None
    r = requests.get(work_id_url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()

def openalex_fetch_citers(work_id: str, max_citers: int) -> List[Dict]:
    """
    Fetch works that cite the given OpenAlex work id.
    Deterministic cap max_citers.
    """
    out: List[Dict] = []
    cursor = "*"
    per_page = 200

    while True:
        params = {
            "filter": f"cites:{work_id}",
            "per-page": per_page,
            "cursor": cursor
        }
        r = requests.get(OPENALEX_API, params=params, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        msg = r.json() or {}
        results = msg.get("results", []) or []
        next_cursor = (msg.get("meta", {}) or {}).get("next_cursor", None)

        if not results:
            break

        out.extend(results)
        if len(out) >= max_citers:
            out = out[:max_citers]
            break

        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    return out

def _openalex_abstract_from_inverted(inv: Optional[Dict]) -> str:
    if not isinstance(inv, dict):
        return ""
    tokens = []
    for word, positions in inv.items():
        for p in positions:
            tokens.append((p, word))
    tokens.sort(key=lambda x: x[0])
    return " ".join(w for _, w in tokens)

def extract_record_openalex(work: Dict, bucket_id: str, query: str) -> Dict:
    title = work.get("title", "") or ""
    year = work.get("publication_year", "") or ""
    doi_url = (work.get("doi") or "").strip()  # https://doi.org/...
    doi = doi_url.replace("https://doi.org/", "").strip()

    host = work.get("host_venue", {}) or {}
    venue = host.get("display_name", "") or ""

    authors = []
    for a in (work.get("authorships") or []):
        au = a.get("author", {}) or {}
        name = au.get("display_name", "") or ""
        if name:
            authors.append(name)

    abstract = _openalex_abstract_from_inverted(work.get("abstract_inverted_index"))

    url = work.get("id", "") or ""
    url_out = doi_url if doi_url else url

    return {
        "Title": title,
        "Year": int(year) if str(year).isdigit() else "",
        "Venue": venue,
        "DOI": doi,
        "URL": url_out,
        "Authors": "; ".join(authors),
        "Abstract": abstract,
        "BucketID": bucket_id,
        "Query": query,
        "RetrievedAtUTC": utc_now_iso()
    }

def openalex_citation_expand(cfg: Dict, seed_records: List[Dict]) -> List[Dict]:
    """
    Expand via OpenAlex citation graph using DOIs from discovered candidates.
    No hardcoded anchors; fully automated.
    """
    oa_cfg = cfg.get("meta", {}).get("openalex", {}) or {}
    if not oa_cfg.get("enabled", False):
        return []

    max_seed = int(oa_cfg.get("max_seed_papers_per_run", 300))
    max_refs = int(oa_cfg.get("max_refs_per_seed", 80))
    max_citers = int(oa_cfg.get("max_citers_per_seed", 80))

    seeds = [r for r in seed_records if r.get("DOI")]
    seeds = seeds[:max_seed]

    expanded: List[Dict] = []

    for r in seeds:
        doi = r["DOI"]
        try:
            w = openalex_get_by_doi(doi)
        except Exception:
            continue
        if not w:
            continue

        # Backward citations: referenced works
        ref_ids = (w.get("referenced_works") or [])[:max_refs]
        for ref_id in ref_ids:
            try:
                rw = openalex_fetch_work_by_id(ref_id)
                if rw:
                    expanded.append(extract_record_openalex(rw, bucket_id="OA_REF", query=doi))
            except Exception:
                pass

        _oa_polite_sleep(cfg)

        # Forward citations: works that cite this work
        try:
            citers = openalex_fetch_citers(w.get("id", ""), max_citers=max_citers)
            for c in citers:
                expanded.append(extract_record_openalex(c, bucket_id="OA_CITER", query=doi))
        except Exception:
            pass

        _oa_polite_sleep(cfg)

    return expanded

# -----------------------------
# Tagging + Routing
# -----------------------------
@dataclass
class TagRule:
    include_any: List[str]
    exclude_any: List[str]

def compile_taxonomy(tax: Dict) -> Tuple[Dict[str, TagRule], List[str], List[str]]:
    tags = {}
    for tag_name, spec in (tax.get("tags") or {}).items():
        tags[tag_name] = TagRule(
            include_any=[s.lower() for s in (spec.get("include_any") or [])],
            exclude_any=[s.lower() for s in (spec.get("exclude_any") or [])]
        )

    routing = tax.get("routing") or {}
    high_any = routing.get("high_confidence_any") or []
    review_any = routing.get("review_queue_any") or []
    return tags, high_any, review_any

def match_rule(text: str, rule: TagRule) -> bool:
    tl = text.lower()
    if rule.exclude_any:
        for x in rule.exclude_any:
            if x and x in tl:
                return False
    if rule.include_any:
        return any((k in tl) for k in rule.include_any if k)
    return False

def tag_record(rec: Dict, tag_rules: Dict[str, TagRule]) -> List[str]:
    blob = safe_join([
        rec.get("Title", ""),
        rec.get("Abstract", ""),
        rec.get("Venue", ""),
        rec.get("BucketID", "")
    ], sep=" || ")

    matched = []
    for tag_name, rule in tag_rules.items():
        if match_rule(blob, rule):
            matched.append(tag_name)
    return matched

def route(tags: List[str], high_any: List[str], review_any: List[str]) -> str:
    if any(t in tags for t in high_any):
        return "MASTER"
    if any(t in tags for t in review_any):
        return "REVIEW"
    # If no node-B evidence, keep it out (deterministic drop)
    return "DROP"

# -----------------------------
# IO: Excel + Logs
# -----------------------------
MASTER_COLUMNS = [
    "Title", "Year", "Venue", "Authors", "DOI", "URL",
    "Tags", "BucketID", "Query", "RetrievedAtUTC", "Abstract"
]

QUEUE_COLUMNS = MASTER_COLUMNS + ["Reason"]

def read_existing(path: Path, cols: List[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_excel(path)
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    return df[cols]

def write_excel(path: Path, df: pd.DataFrame):
    df.to_excel(path, index=False)

def append_runlog(rows: List[Dict]):
    if not rows:
        return
    df_new = pd.DataFrame(rows)
    if RUNLOG_CSV.exists():
        df_old = pd.read_csv(RUNLOG_CSV)
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df.to_csv(RUNLOG_CSV, index=False)

def load_last_ts() -> Optional[str]:
    if not LAST_TS_PATH.exists():
        return None
    return LAST_TS_PATH.read_text(encoding="utf-8").strip() or None

def save_last_ts(ts: str):
    LAST_TS_PATH.write_text(ts, encoding="utf-8")

# -----------------------------
# Dedup
# -----------------------------
def dedup_records(records: List[Dict], existing_master: pd.DataFrame, existing_queue: pd.DataFrame) -> List[Dict]:
    seen_doi = set(str(x).strip().lower() for x in pd.concat([existing_master["DOI"], existing_queue["DOI"]], ignore_index=True).fillna(""))
    seen_title = set(normalize_title(x) for x in pd.concat([existing_master["Title"], existing_queue["Title"]], ignore_index=True).fillna(""))

    out = []
    for r in records:
        doi = (r.get("DOI") or "").strip().lower()
        nt = normalize_title(r.get("Title") or "")
        if doi and doi in seen_doi:
            continue
        if nt and nt in seen_title:
            continue
        if doi:
            seen_doi.add(doi)
        if nt:
            seen_title.add(nt)
        out.append(r)
    return out

# -----------------------------
# Node-B Export
# -----------------------------
def export_node_b(master_df: pd.DataFrame, queue_df: pd.DataFrame):
    out_dir = REPORT_DIR / "node_b"
    out_dir.mkdir(parents=True, exist_ok=True)

    def has_b_tag(tag_str: str) -> bool:
        ts = (tag_str or "").lower()
        return ("b1_monolithic_ic_platform" in ts) or ("b2_cmos_logic_demo" in ts) or ("b3_non_cmos_logic_context" in ts)

    m = master_df[master_df["Tags"].apply(has_b_tag)].copy()
    q = queue_df[queue_df["Tags"].apply(has_b_tag)].copy()

    write_excel(out_dir / "NodeB_HighConfidence.xlsx", m)
    write_excel(out_dir / "NodeB_Candidates_ReviewQueue.xlsx", q)

# -----------------------------
# Main
# -----------------------------
def main():
    ensure_dirs()

    cfg = load_json(QUERIES_PATH)
    tax = load_yaml(TAXONOMY_PATH)

    tags_rules, high_any, review_any = compile_taxonomy(tax)
    year_min = int(cfg.get("meta", {}).get("year_min", DEFAULT_YEAR_MIN))

    # Crossref paging controls
    crossref_meta = cfg.get("meta", {}).get("crossref", {}) or {}
    rows_per_page = int(crossref_meta.get("rows_per_page", CROSSREF_ROWS_PER_QUERY))
    max_items_per_query = int(crossref_meta.get("max_items_per_query", 2000))
    crossref_delay = float(crossref_meta.get("polite_delay_sec", 1.0))

    master_df = read_existing(MASTER_XLSX, MASTER_COLUMNS)
    queue_df = read_existing(QUEUE_XLSX, QUEUE_COLUMNS)

    all_new_records: List[Dict] = []
    runlog_rows: List[Dict] = []

    # -----------------------------
    # Pass 1: Bucket keyword discovery
    # -----------------------------
    for bucket in cfg.get("buckets", []):
        bucket_id = bucket.get("bucket_id", "UNKNOWN_BUCKET")
        for q in bucket.get("queries", []):
            q = str(q).strip()
            if not q:
                continue

            try:
                items = crossref_search_cursor(q, year_min=year_min, rows=rows_per_page, max_items=max_items_per_query)
            except Exception as e:
                runlog_rows.append({
                    "RunAtUTC": utc_now_iso(),
                    "BucketID": bucket_id,
                    "Query": q,
                    "Status": "ERROR",
                    "Message": str(e)
                })
                continue

            for it in items:
                rec = extract_record(it, bucket_id=bucket_id, query=q)
                if not rec["DOI"] and not rec["URL"]:
                    continue
                if rec["Year"] and rec["Year"] < year_min:
                    continue
                all_new_records.append(rec)

            runlog_rows.append({
                "RunAtUTC": utc_now_iso(),
                "BucketID": bucket_id,
                "Query": q,
                "Status": "OK",
                "ReturnedItems": len(items)
            })

            time.sleep(crossref_delay)

    # -----------------------------
    # Pass 2: Venue sweeps (recall boost for flagship venues)
    # -----------------------------
    for sweep in cfg.get("meta", {}).get("venue_sweeps", []) or []:
        venue_id = sweep.get("venue_id", "VENUE")
        sweep_year_min = int(sweep.get("year_min", year_min))
        sweep_max_items = int(sweep.get("max_items", 5000))
        for term in (sweep.get("query_terms", []) or []):
            q = f"\"{term}\""
            try:
                items = crossref_search_cursor(q, year_min=sweep_year_min, rows=rows_per_page, max_items=sweep_max_items)
            except Exception as e:
                runlog_rows.append({
                    "RunAtUTC": utc_now_iso(),
                    "BucketID": f"VENUE_SWEEP_{venue_id}",
                    "Query": q,
                    "Status": "ERROR",
                    "Message": str(e)
                })
                continue

            for it in items:
                rec = extract_record(it, bucket_id=f"VENUE_SWEEP_{venue_id}", query=q)
                if not rec["DOI"] and not rec["URL"]:
                    continue
                if rec["Year"] and rec["Year"] < year_min:
                    continue
                all_new_records.append(rec)

            runlog_rows.append({
                "RunAtUTC": utc_now_iso(),
                "BucketID": f"VENUE_SWEEP_{venue_id}",
                "Query": q,
                "Status": "OK",
                "ReturnedItems": len(items)
            })

            time.sleep(crossref_delay)

    # Dedup against existing
    all_new_records = dedup_records(all_new_records, master_df, queue_df)

    # -----------------------------
    # Pass 3: OpenAlex citation expansion (automatic recall for highly relevant papers)
    # -----------------------------
    try:
        oa_records = openalex_citation_expand(cfg, all_new_records)
        all_new_records.extend(oa_records)
    except Exception as e:
        runlog_rows.append({
            "RunAtUTC": utc_now_iso(),
            "BucketID": "OPENALEX_EXPAND",
            "Query": "",
            "Status": "ERROR",
            "Message": str(e)
        })

    # Dedup again after expansion
    all_new_records = dedup_records(all_new_records, master_df, queue_df)

    # Tag + route
    master_add = []
    queue_add = []

    for r in all_new_records:
        matched_tags = tag_record(r, tags_rules)
        r["Tags"] = "; ".join(matched_tags)

        decision = route(matched_tags, high_any, review_any)
        if decision == "MASTER":
            master_add.append({k: r.get(k, "") for k in MASTER_COLUMNS})
        elif decision == "REVIEW":
            rr = {k: r.get(k, "") for k in QUEUE_COLUMNS}
            rr["Reason"] = "Ambiguous Node-B evidence (B3-only or weak match)"
            queue_add.append(rr)
        else:
            pass  # DROP

    if master_add:
        master_df = pd.concat([master_df, pd.DataFrame(master_add)], ignore_index=True)

    if queue_add:
        queue_df = pd.concat([queue_df, pd.DataFrame(queue_add)], ignore_index=True)

    write_excel(MASTER_XLSX, master_df)
    write_excel(QUEUE_XLSX, queue_df)
    append_runlog(runlog_rows)

    export_node_b(master_df, queue_df)

    save_last_ts(utc_now_iso())
    print("Run complete.")
    print("Master:", MASTER_XLSX)
    print("Review:", QUEUE_XLSX)
    print("Reports:", REPORT_DIR / "node_b")

if __name__ == "__main__":
    main()
