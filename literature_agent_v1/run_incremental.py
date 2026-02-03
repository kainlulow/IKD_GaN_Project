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

DEFAULT_YEAR_MIN = 2010
CROSSREF_ROWS_PER_QUERY = 200  # keep modest for GH actions
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

# -----------------------------
# Crossref Querying
# -----------------------------
def crossref_search(query: str, year_min: int) -> List[Dict]:
    """
    Query Crossref works. Deterministic order is not guaranteed by Crossref;
    we rely on dedup + audit logging, and incremental time checkpoint.
    """
    params = {
        "query": query,
        "filter": f"from-pub-date:{year_min}-01-01",
        "rows": CROSSREF_ROWS_PER_QUERY
    }
    r = requests.get(CROSSREF_API, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return data.get("message", {}).get("items", [])

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

    abstract = item.get("abstract", "") or ""
    abstract = re.sub(r"<[^>]+>", " ", abstract)  # strip jats tags if present
    abstract = re.sub(r"\s+", " ", abstract).strip()

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
        # update sets
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

    master_df = read_existing(MASTER_XLSX, MASTER_COLUMNS)
    queue_df = read_existing(QUEUE_XLSX, QUEUE_COLUMNS)

    all_new_records: List[Dict] = []
    runlog_rows: List[Dict] = []

    for bucket in cfg.get("buckets", []):
        bucket_id = bucket.get("bucket_id", "UNKNOWN_BUCKET")
        for q in bucket.get("queries", []):
            q = str(q).strip()
            if not q:
                continue

            try:
                items = crossref_search(q, year_min=year_min)
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
                # Deterministic validation: require DOI or URL
                if not rec["DOI"] and not rec["URL"]:
                    continue
                # Basic year filter
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

            time.sleep(1.0)  # be polite to Crossref

    # Dedup against existing
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
            # DROP silently (still auditable via RunLog OK lines + results count)
            pass

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
