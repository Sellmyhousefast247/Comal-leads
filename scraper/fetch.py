#!/usr/bin/env python3
"""
Comal County (Texas) Motivated Seller Lead Scraper
===================================================
Comal's recorder (ROAM, comal.landrecordsonline.com) requires an account
even for its $0 tier, so this build is court + posting based:

Sources
  1. Tyler Odyssey Public Access (anonymous, HTTP):
       http://public.co.comal.tx.us/  ->  Search.aspx?ID=700 (civil/family)
                                          Search.aspx?ID=200 (probate)
     Date-filed searches return ALL rows on one page (no pagination,
     verified at 168 rows). Session note: Search.aspx must be reached by
     clicking the launch link on default.aspx (sets server session);
     deep-linking redirects back to default.aspx.
     Civil types kept: Tax Cases -> TAXFC, Real Property -> LP,
     Suits on Debt / Debt Claim -> JUD. Probate: PC-prefixed cases -> PRO
     (decedent parsed from "ESTATE OF <name>[, DECEASED]").
  2. Monthly foreclosure-sale posting PDF (scanned images) at
     https://www.comalcounty.gov/213/Foreclosure-Sales -> OCR via
     pdf2image + pytesseract; best-effort address / mortgagor / sale-date
     extraction. cat FC. Failure of this source never zeroes the run.
  3. Enrichment: Comal CAD parcels FeatureServer (BIS webmap proxy)
     https://utility.arcgis.com/usrsvcs/servers/5d37dc8436c24c70aa3cdcec26923b60/
       rest/services/ComalCADWebService/FeatureServer/0
     ~106,800 parcels. file_as_name = "LAST FIRST M" uppercase; situs in
     separate fields (situs_num/_street/_city/_zip); mailing in
     addr_line1..3/addr_city/addr_state/zip (first non-empty line wins).
     Send a Referer header (the BIS app URL) with every query.

Future upgrade: ROAM free "Pay-As-You-Go" account -> recorder index
(lis pendens, liens, heirship, trustee notices) once credentials exist.

Run:
    python scraper/fetch.py                # default 7-day lookback
    python scraper/fetch.py --days 14
    python scraper/fetch.py --skip-parcel
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
COUNTY = "Comal"
STATE = "TX"
ODYSSEY_BASE = "http://public.co.comal.tx.us"
FC_PAGE_URL = "https://www.comalcounty.gov/213/Foreclosure-Sales"
FC_SITE = "https://www.comalcounty.gov"
PARCEL_API_URL = ("https://utility.arcgis.com/usrsvcs/servers/"
                  "5d37dc8436c24c70aa3cdcec26923b60/rest/services/"
                  "ComalCADWebService/FeatureServer/0/query")
PARCEL_REFERER = "https://gis.bisclient.com/comalcad/"

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
PROBATE_LOOKBACK_DAYS = 60
REQUEST_TIMEOUT = 30
ARCGIS_MAX_LOOKUPS = 1500
OCR_DPI = 200
OCR_MAX_PAGES = 400

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("comal_scraper")

# Civil case types -> (cat, label). Anything else is skipped.
CIVIL_TYPE_MAP = [
    (re.compile(r"tax", re.I),                       "TAXFC", "Delinquent Tax Suit"),
    (re.compile(r"real property|foreclos|quiet title|trespass to try|partition", re.I),
                                                     "LP",    "Real Property Suit"),
    (re.compile(r"suits on debt|debt claim|debt/contract", re.I),
                                                     "JUD",   "Debt Suit"),
]
PROBATE_CASE_RE = re.compile(r"^\d{4}PC\d+", re.I)
PROBATE_CATS = {"PRO"}

GHL_FIELDS = [
    "doc_num","doc_type","cat","cat_label","filed","owner","grantee",
    "amount","prop_address","prop_city","prop_state","prop_zip",
    "mail_address","mail_city","mail_state","mail_zip","legal","clerk_url","score","flags",
    "first_seen","status",
]
GHL_HEADERS = {f: f.replace("_", " ").title() for f in GHL_FIELDS}
GHL_HEADERS["first_seen"] = "Date Entered System"
GHL_HEADERS["status"] = "Status"

@dataclass
class LeadRecord:
    doc_num: str = ""
    doc_type: str = ""
    cat: str = ""
    cat_label: str = ""
    filed: str = ""
    owner: str = ""
    grantee: str = ""
    amount: float = 0.0
    legal: str = ""
    prop_address: str = ""
    prop_city: str = ""
    prop_state: str = STATE
    prop_zip: str = ""
    mail_address: str = ""
    mail_city: str = ""
    mail_state: str = STATE
    mail_zip: str = ""
    clerk_url: str = ""
    flags: list = field(default_factory=list)
    score: int = 0
    status: str = ""
    first_seen: str = ""
    rid: str = ""
    content_hash: str = ""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalize_date(raw: str) -> str:
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw.strip()


def _norm_ws(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip())


def _arc_val(x) -> str:
    s = _norm_ws(x)
    return "" if s.upper() in ("NULL", "NONE") else s


def _sql_lit(s: str) -> str:
    return s.upper().replace("'", "''")


def normalize_owner_for_parcel(name: str) -> str:
    """CAD file_as_name is 'LAST FIRST M' uppercase.

    Court parties come as 'Last, First M' (probate decedents) or
    'FIRST LAST' / entity names (civil defendants). Produce a
    'LAST FIRST' two-token prefix for LIKE matching.
    """
    if not name:
        return ""
    n = _norm_ws(name).upper()
    n = re.sub(r"\b(JR|SR|II|III|IV)\.?$", "", n).strip().rstrip(",")
    n = re.sub(r"\b(AKA|A/K/A|DBA|D/B/A)\b.*$", "", n).strip().rstrip(",")
    if "," in n:
        last, first = n.split(",", 1)
        first_tok = first.strip().split()
        return f"{last.strip()} {first_tok[0] if first_tok else ''}".strip()
    parts = n.split()
    if len(parts) >= 2:
        # Civil styles are usually FIRST [M] LAST -> flip to LAST FIRST
        return f"{parts[-1]} {parts[0]}"
    return n

# ---------------------------------------------------------------------------
# Odyssey Public Access scraper (Playwright)
# ---------------------------------------------------------------------------
class OdysseyScraper:
    """Anonymous Tyler Odyssey PA. Flow per search:
    default.aspx -> click launch link (sets session) -> fill Date Filed
    range -> submit -> parse the single results page."""

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) "
           "Chrome/124.0.0.0 Safari/537.36")

    LAUNCH = {
        "civil":   "Civil, Family Case Records",
        "probate": "Probate Case Records",
    }

    def __init__(self, civil_start: datetime, probate_start: datetime,
                 end: datetime):
        self.civil_start = civil_start
        self.probate_start = probate_start
        self.end = end

    # ---- parsing ----
    @staticmethod
    def _parse_results(html: str):
        """Yield dicts from a results page: every row holding a
        CaseDetail link. Cells: [case#][?][style][filed\ncourt][type\nstatus]
        (probate lacks the extra empty cell -> detect by content)."""
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=re.compile(r"CaseDetail\.aspx", re.I)):
            tr = a.find_parent("tr")
            if not tr:
                continue
            tds = [td.get_text("\n", strip=True) for td in tr.find_all("td")]
            if len(tds) < 4:
                continue
            case_no = tds[0].split("\n")[0].strip()
            # find the filed-date cell (starts MM/DD/YYYY)
            filed = court = ctype = status = style = ""
            for i, cell in enumerate(tds[1:], start=1):
                m = re.match(r"(\d{2}/\d{2}/\d{4})", cell)
                if m:
                    filed = m.group(1)
                    parts = cell.split("\n")
                    court = parts[1].strip() if len(parts) > 1 else ""
                    nxt = tds[i + 1] if i + 1 < len(tds) else ""
                    tparts = nxt.split("\n")
                    ctype = tparts[0].strip()
                    status = tparts[1].strip() if len(tparts) > 1 else ""
                    style = max(tds[1:i], key=len).strip() if i > 1 else ""
                    break
            if not case_no or not filed:
                continue
            yield {
                "case_no": case_no, "style": style, "filed": filed,
                "court": court, "type": ctype, "status": status,
                "href": a.get("href", ""),
            }

    @staticmethod
    def _split_style(style: str):
        """'PLAINTIFF vs DEFENDANT' -> (defendant/owner, plaintiff)."""
        m = re.split(r"\s+vs\.?\s+|\s+VS\.?\s+|\s+Vs\.?\s+", style, maxsplit=1)
        if len(m) == 2:
            plaintiff, defendant = m[0].strip(), m[1].strip()
            defendant = re.sub(r",?\s*(et al\.?|ET AL\.?).*$", "", defendant).strip()
            return defendant, plaintiff
        return style.strip(), ""

    @staticmethod
    def _decedent(style: str) -> str:
        m = re.search(r"ESTATE OF\s+(.+?)(?:,?\s*DECEASED)?$", style, re.I)
        name = (m.group(1) if m else style).strip().rstrip(",")
        return name

    # ---- searching ----
    def _run_search(self, page, launch_text: str, start: datetime,
                    end: datetime) -> str:
        page.goto(f"{ODYSSEY_BASE}/default.aspx",
                  wait_until="domcontentloaded", timeout=30000)
        page.wait_for_selector(f"text={launch_text}", timeout=20000)
        page.click(f"text={launch_text}")
        page.wait_for_selector("#DateFiled", timeout=30000)
        page.click("#DateFiled")
        page.fill("#DateFiledOnAfter", start.strftime("%m/%d/%Y"))
        page.fill("#DateFiledOnBefore", end.strftime("%m/%d/%Y"))
        page.click("#SearchSubmit")
        page.wait_for_selector("text=Record Count", timeout=60000)
        return page.content()

    def run(self) -> list:
        records = []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=self._UA)
            page = ctx.new_page()

            # ---- civil ----
            try:
                html = self._run_search(page, self.LAUNCH["civil"],
                                        self.civil_start, self.end)
                n = 0
                for row in self._parse_results(html):
                    cat = label = None
                    for rx, c, lb in CIVIL_TYPE_MAP:
                        if rx.search(row["type"]):
                            cat, label = c, lb
                            break
                    if not cat:
                        continue
                    owner, plaintiff = self._split_style(row["style"])
                    records.append(LeadRecord(
                        doc_num=row["case_no"], doc_type=row["type"],
                        cat=cat, cat_label=label,
                        filed=normalize_date(row["filed"]),
                        owner=owner, grantee=plaintiff,
                        legal=f"{row['court']} - {row['status']}".strip(" -"),
                        clerk_url=f"{ODYSSEY_BASE}/{row['href']}",
                    ))
                    n += 1
                log.info("Odyssey civil: %d motivated-seller cases", n)
            except Exception as exc:
                log.warning("Odyssey civil search failed: %s", exc)

            # ---- probate ----
            try:
                html = self._run_search(page, self.LAUNCH["probate"],
                                        self.probate_start, self.end)
                n = 0
                for row in self._parse_results(html):
                    if not PROBATE_CASE_RE.match(row["case_no"]):
                        continue   # skip inquests (IN...), guardianships etc.
                    records.append(LeadRecord(
                        doc_num=row["case_no"], doc_type=row["type"] or "Probate",
                        cat="PRO", cat_label=row["type"] or "Probate / Estate",
                        filed=normalize_date(row["filed"]),
                        owner=self._decedent(row["style"]),
                        legal=f"{row['court']} - {row['status']}".strip(" -"),
                        clerk_url=f"{ODYSSEY_BASE}/{row['href']}",
                    ))
                    n += 1
                log.info("Odyssey probate: %d estates", n)
            except Exception as exc:
                log.warning("Odyssey probate search failed: %s", exc)

            browser.close()
        return records

# ---------------------------------------------------------------------------
# Monthly foreclosure posting PDF (scanned -> OCR)
# ---------------------------------------------------------------------------
STREET_RE = re.compile(
    r"\b(\d{2,6}\s+[A-Z][A-Za-z0-9 .'-]{2,40}?"
    r"(?:ROAD|RD|STREET|ST|DRIVE|DR|LANE|LN|COURT|CT|CIRCLE|CIR|TRAIL|TRL|"
    r"AVENUE|AVE|BOULEVARD|BLVD|WAY|PASS|PATH|LOOP|RUN|COVE|CV|BEND|PARK|"
    r"PKWY|PARKWAY|HOLLOW|HL|HILL|HILLS|RIDGE|CANYON|CREEK|VALLEY|VIEW|"
    r"CROSSING|XING|TERRACE|TER|PLACE|PL|POINT|PT|SPRINGS?|MEADOWS?|OAKS?))"
    r"\b[,.]?\s*(?:#?\s*\d+[A-Z]?)?", re.I)
ZIP_RE = re.compile(r"\b(NEW BRAUNFELS|SPRING BRANCH|CANYON LAKE|BULVERDE|"
                    r"FISCHER|GARDEN RIDGE|SCHERTZ|SELMA)\b[^0-9]{0,20}(\d{5})", re.I)
SALE_DATE_RE = re.compile(r"(January|February|March|April|May|June|July|August|"
                          r"September|October|November|December)\s+(\d{1,2}),?\s+(20\d{2})", re.I)


def fetch_fc_pdf_records(session) -> list:
    """Download the current month's posted trustee-sale PDF and OCR it.
    Each OCR page that looks like a notice becomes one cat=FC record with
    best-effort address / mortgagor. Never raises."""
    records = []
    try:
        r = session.get(FC_PAGE_URL, timeout=REQUEST_TIMEOUT)
        m = re.search(r'href="(/DocumentCenter/View/\d+/[^"]*Foreclosure[^"]*)"',
                      r.text, re.I)
        if not m:
            log.warning("FC PDF link not found on posting page")
            return records
        pdf_url = FC_SITE + m.group(1)
        month_label = re.search(r"View/\d+/([A-Za-z]+)", pdf_url)
        month_label = month_label.group(1) if month_label else "Current"
        log.info("FC posting PDF: %s", pdf_url)
        pdf_bytes = session.get(pdf_url, timeout=120).content
        log.info("FC PDF downloaded: %d bytes", len(pdf_bytes))

        from pdf2image import convert_from_bytes
        import pytesseract
        pages = convert_from_bytes(pdf_bytes, dpi=OCR_DPI)[:OCR_MAX_PAGES]
        log.info("FC PDF pages: %d (OCR at %d dpi)", len(pages), OCR_DPI)

        # OCR every page; group consecutive pages into notices whenever a
        # page contains a "NOTICE OF ... SALE" header, else attach to the
        # previous notice.
        notices = []
        for idx, img in enumerate(pages, start=1):
            try:
                text = pytesseract.image_to_string(img)
            except Exception as exc:
                log.debug("OCR failed p%d: %s", idx, exc)
                continue
            if re.search(r"NOTICE\s+OF.{0,40}SALE", text, re.I) or not notices:
                notices.append({"page": idx, "text": text})
            else:
                notices[-1]["text"] += "\n" + text

        for n in notices:
            text = n["text"]
            addr_m = STREET_RE.search(text.upper())
            zip_m = ZIP_RE.search(text)
            date_m = SALE_DATE_RE.search(text)
            mort_m = re.search(r"(?:executed by|Grantor\(?s?\)?:?)\s+([A-Z][A-Za-z ,.'-]{4,60})",
                               text)
            sale = ""
            if date_m:
                try:
                    sale = datetime.strptime(
                        f"{date_m.group(1)} {date_m.group(2)} {date_m.group(3)}",
                        "%B %d %Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            rec = LeadRecord(
                doc_num=f"FCPDF-{month_label}-p{n['page']}",
                doc_type="NOTICE OF TRUSTEE SALE",
                cat="FC",
                cat_label=(f"Trustee Sale {sale}" if sale else "Trustee Sale Posting"),
                filed=datetime.now().strftime("%Y-%m-%d"),
                owner=_norm_ws(mort_m.group(1)) if mort_m else "",
                prop_address=_norm_ws(addr_m.group(1)).title() if addr_m else "",
                prop_city=_norm_ws(zip_m.group(1)).title() if zip_m else "",
                prop_zip=zip_m.group(2) if zip_m else "",
                legal=(f"Trustee sale date: {sale}. " if sale else "") +
                      f"OCR of posted notice, page {n['page']}",
                clerk_url=pdf_url,
            )
            records.append(rec)
        log.info("FC posting PDF: %d notices extracted (%d with address)",
                 len(records), sum(1 for r in records if r.prop_address))
    except Exception as exc:
        log.warning("FC PDF source failed (skipping): %s", exc)
    return records

# ---------------------------------------------------------------------------
# Comal CAD parcel enrichment (ArcGIS via BIS proxy)
# ---------------------------------------------------------------------------
PARCEL_FIELDS = ("file_as_name,situs_num,situs_street_prefx,situs_street,"
                 "situs_street_sufix,situs_city,situs_zip,addr_line1,"
                 "addr_line2,addr_line3,addr_city,addr_state,zip,market,prop_id")


def _situs_from(att: dict) -> tuple:
    street = " ".join(x for x in (
        _arc_val(att.get("situs_num")), _arc_val(att.get("situs_street_prefx")),
        _arc_val(att.get("situs_street")), _arc_val(att.get("situs_street_sufix"))) if x)
    return street, _arc_val(att.get("situs_city")).title(), _arc_val(att.get("situs_zip"))


def _mailing_from(att: dict) -> tuple:
    street = (_arc_val(att.get("addr_line1")) or _arc_val(att.get("addr_line2"))
              or _arc_val(att.get("addr_line3")))
    return (street, _arc_val(att.get("addr_city")).title(),
            _arc_val(att.get("addr_state")) or STATE, _arc_val(att.get("zip"))[:5])


def _arcgis_query(session, where: str, count: int = 5) -> list:
    params = {
        "where": where, "outFields": PARCEL_FIELDS,
        "returnGeometry": "false", "f": "json", "resultRecordCount": count,
    }
    try:
        r = session.get(PARCEL_API_URL, params=params, timeout=REQUEST_TIMEOUT,
                        headers={"Referer": PARCEL_REFERER})
        return r.json().get("features", []) or []
    except Exception as exc:
        log.debug("ArcGIS query error: %s", exc)
        return []


def _addr_key(addr: str) -> tuple:
    m = re.match(r"\s*(\d+)\s+(.*)", addr or "")
    if not m:
        return "", ""
    rest = _norm_ws(m.group(2))
    rest = re.sub(r"\s+(#|APT|UNIT|STE|SUITE|BLDG|LOT)\b.*$", "", rest, flags=re.I)
    # situs_street excludes the suffix; drop a trailing suffix word
    rest = re.sub(r"\s+(RD|ROAD|ST|STREET|DR|DRIVE|LN|LANE|CT|COURT|CIR|CIRCLE|"
                  r"TRL|TRAIL|AVE|AVENUE|BLVD|WAY|PASS|PATH|LOOP|RUN|CV|COVE|"
                  r"BND|BEND|PKWY|PARKWAY|TER|TERRACE|PL|PLACE|PT|POINT|XING|"
                  r"CROSSING)\.?$", "", rest, flags=re.I).strip()
    return m.group(1), rest


def enrich_parcels(records: list) -> None:
    session = requests.Session()
    session.headers["User-Agent"] = "ComalLeadScraper/1.0"

    fwd = [r for r in records if r.owner and (not r.prop_address or not r.mail_address)]
    log.info("CAD owner-lookup for %d records...", len(fwd))
    hits = 0
    for rec in fwd[:ARCGIS_MAX_LOOKUPS]:
        norm = normalize_owner_for_parcel(rec.owner)
        if not norm or len(norm) < 5:
            continue
        feats = _arcgis_query(session, f"UPPER(file_as_name) LIKE '{_sql_lit(norm)}%'")
        if not feats:
            continue
        att = feats[0].get("attributes", {})
        if not rec.prop_address and len(feats) == 1:
            ps, pc, pz = _situs_from(att)
            if ps:
                rec.prop_address, rec.prop_city, rec.prop_zip = ps, pc or rec.prop_city, pz
        if not rec.mail_address:
            ms, mc, mst, mz = _mailing_from(att)
            if ms:
                rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
                hits += 1
        if not rec.amount:
            try:
                rec.amount = float(att.get("market") or 0)
            except (TypeError, ValueError):
                pass
        time.sleep(0.12)
    log.info("CAD owner-lookup: %d mailing fills", hits)

    rev = [r for r in records if r.prop_address and not r.owner]
    log.info("CAD address-lookup for %d records...", len(rev))
    hits = 0
    for rec in rev[:ARCGIS_MAX_LOOKUPS]:
        num, core = _addr_key(rec.prop_address)
        if not num or not core:
            continue
        feats = _arcgis_query(
            session, f"situs_num = '{_sql_lit(num)}' AND "
                     f"UPPER(situs_street) LIKE '{_sql_lit(core)}%'")
        if len(feats) == 1:
            att = feats[0].get("attributes", {})
            owner = _arc_val(att.get("file_as_name"))
            if owner:
                rec.owner = owner
                hits += 1
            if not rec.mail_address:
                ms, mc, mst, mz = _mailing_from(att)
                if ms:
                    rec.mail_address, rec.mail_city, rec.mail_state, rec.mail_zip = ms, mc, mst, mz
        time.sleep(0.12)
    log.info("CAD address-lookup: %d owner fills", hits)

# ---------------------------------------------------------------------------
# Hash / dedupe + NEW-CHANGED detection
# ---------------------------------------------------------------------------
def _repo_base() -> Path:
    return Path(__file__).parent.parent


def _record_rid(r) -> str:
    basis = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}|{r.prop_address}"
    return hashlib.sha1(f"comal|{basis}".encode()).hexdigest()[:16]


def _record_chash(r) -> str:
    fields = "|".join(str(x or "") for x in (
        r.doc_num, r.doc_type, r.filed, r.owner, r.grantee, r.legal,
        r.amount, r.prop_address, r.mail_address))
    return hashlib.sha1(fields.encode()).hexdigest()[:16]


def detect_changes(records: list) -> None:
    state_path = _repo_base() / "data" / "state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    today = datetime.now().strftime("%Y-%m-%d")
    n_new = n_chg = n_exist = 0
    for r in records:
        r.rid = _record_rid(r)
        r.content_hash = _record_chash(r)
        prev = state.get(r.rid)
        if prev is None:
            r.status, r.first_seen = "NEW", today
            n_new += 1
        elif prev.get("content_hash") != r.content_hash:
            r.status = "CHANGED"
            r.first_seen = prev.get("first_seen", today)
            n_chg += 1
        else:
            r.status = "EXISTING"
            r.first_seen = prev.get("first_seen", today)
            n_exist += 1
        state[r.rid] = {"content_hash": r.content_hash,
                        "first_seen": r.first_seen, "last_seen": today}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
    log.info("NEW/CHANGED: NEW=%d CHANGED=%d EXISTING=%d (state=%d ids)",
             n_new, n_chg, n_exist, len(state))

# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def score_records(records: list, start: datetime) -> None:
    for r in records:
        s, flags = 30, []
        if r.cat == "LP": s += 10; flags.append("LIS_PENDENS")
        if r.cat == "FC": s += 15; flags.append("FORECLOSURE")
        if r.cat == "TAXFC": s += 18; flags.append("TAX_FORECLOSURE")
        if r.cat == "TAXDEED": s += 10; flags.append("TAX_DEED")
        if r.cat in ("LP","FC","TAXFC"): s += 5
        if r.cat == "JUD": s += 8; flags.append("JUDGMENT")
        if r.cat == "LIEN": s += 7; flags.append("LIEN")
        if r.cat == "PRO": s += 12; flags.append("PROBATE")
        if r.amount > 100000: s += 15; flags.append("HIGH_AMOUNT")
        elif r.amount > 50000: s += 10; flags.append("MID_AMOUNT")
        if r.filed:
            try:
                if datetime.strptime(r.filed, "%Y-%m-%d") >= start:
                    s += 5; flags.append("NEW_THIS_WEEK")
            except ValueError:
                pass
        if r.prop_address:
            s += 5; flags.append("HAS_ADDRESS")
        r.score = min(s, 100)
        r.flags = flags

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
DASH_CAT = {
    "LP": "foreclosure", "FC": "foreclosure", "TAXFC": "foreclosure",
    "TAXDEED": "tax_lien", "LIEN": "tax_lien",
    "JUD": "judgment", "PRO": "probate",
}
FLAG_NICE = {
    "LIS_PENDENS": "Lis pendens", "FORECLOSURE": "Pre-foreclosure",
    "TAX_FORECLOSURE": "Tax foreclosure", "TAX_DEED": "Tax deed",
    "JUDGMENT": "Judgment lien", "LIEN": "Tax lien",
    "PROBATE": "Probate / estate", "HIGH_AMOUNT": "Amount > $100k",
    "MID_AMOUNT": "Amount > $50k", "NEW_THIS_WEEK": "New this week",
    "HAS_ADDRESS": "Has address",
}


def write_outputs(records: list, start: datetime, end: datetime) -> None:
    base = _repo_base()
    for d in [base / "dashboard", base / "data"]:
        d.mkdir(parents=True, exist_ok=True)
    week_ago = (end - timedelta(days=7)).strftime("%Y-%m-%d")
    recs_out = []
    for r in records:
        d = asdict(r)
        d["cat_code"] = r.cat
        d["cat"] = DASH_CAT.get(r.cat, "tax_lien")
        d["flags"] = [FLAG_NICE.get(f, f) for f in (r.flags or [])]
        d["absentee"] = bool(
            r.prop_address and r.mail_address
            and r.prop_address.upper() != r.mail_address.upper())
        d["out_of_state"] = bool(r.mail_state and r.mail_state.upper() != STATE)
        recs_out.append(d)
    payload = {
        "fetched_at": datetime.utcnow().isoformat(),
        "county": COUNTY,
        "source": f"{COUNTY} County, {STATE} -- Odyssey Courts + FC Postings + Comal CAD",
        "date_range": {"start": start.strftime("%Y-%m-%d"), "end": end.strftime("%Y-%m-%d")},
        "total": len(records),
        "new_7d": sum(1 for r in records if (r.first_seen or "") >= week_ago),
        "with_address": sum(1 for r in records if r.prop_address),
        "by_cat": {c: sum(1 for r in records if r.cat == c) for c in ("FC","TAXFC","TAXDEED","LP","JUD","LIEN","PRO")},
        "records": recs_out,
    }
    for path in [base / "dashboard" / "records.json", base / "data" / "records.json"]:
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.info("JSON written: %s (%d records)", path, len(records))
    csv_path = base / "data" / "ghl_export.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(GHL_HEADERS.values()))
        writer.writeheader()
        for r in records:
            d = asdict(r)
            writer.writerow({GHL_HEADERS[k]: ("|".join(d[k]) if k=="flags" else d[k]) for k in GHL_FIELDS})
    log.info("GHL CSV written: %s (%d records)", csv_path, len(records))
    skip_path = base / "data" / "skiptrace_export.csv"
    skip_cols = ["First Name", "Last Name", "Mailing Address", "Mailing City",
                 "Mailing State", "Mailing Zip", "Property Address",
                 "Property City", "Property State", "Property Zip"]
    with open(skip_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=skip_cols)
        writer.writeheader()
        for r in records:
            owner = (r.owner or "").strip()
            if "," in owner:
                p = owner.split(",", 1)
                first, last = p[1].strip().title(), p[0].strip().title()
            else:
                p = owner.split()
                if p and p[0].isupper() and len(p) > 1 and owner == owner.upper():
                    # CAD/clerk "LAST FIRST M" style
                    first, last = p[1].title(), p[0].title()
                else:
                    # court "First Last" style
                    first = p[0].title() if p else ""
                    last = p[-1].title() if len(p) > 1 else ""
            writer.writerow({
                "First Name": first, "Last Name": last,
                "Mailing Address": r.mail_address, "Mailing City": r.mail_city,
                "Mailing State": r.mail_state, "Mailing Zip": r.mail_zip,
                "Property Address": r.prop_address, "Property City": r.prop_city,
                "Property State": r.prop_state, "Property Zip": r.prop_zip,
            })
    log.info("Skip trace CSV written: %s (%d records)", skip_path, len(records))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Comal County lead scraper")
    parser.add_argument("--days", type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--probate-days", type=int, default=PROBATE_LOOKBACK_DAYS)
    parser.add_argument("--skip-parcel", action="store_true")
    parser.add_argument("--skip-fcpdf", action="store_true")
    args = parser.parse_args()
    end = datetime.now()
    start = end - timedelta(days=args.days)
    probate_start = end - timedelta(days=args.probate_days)
    log.info("=" * 60)
    log.info("Comal County Motivated Seller Lead Scraper")
    log.info("Lookback default=%dd  probate=%dd", args.days, args.probate_days)
    log.info("=" * 60)
    log.info("Range: civil %s->%s | probate %s->%s",
             start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"),
             probate_start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"))

    scraper = OdysseyScraper(start, probate_start, end)
    records = scraper.run()

    if not args.skip_fcpdf:
        session = requests.Session()
        session.headers["User-Agent"] = OdysseyScraper._UA
        fc = fetch_fc_pdf_records(session)
        seen_docs = {r.doc_num for r in records}
        records.extend(r for r in fc if r.doc_num not in seen_docs)

    # dedupe on doc_num
    seen, unique = set(), []
    for r in records:
        key = r.doc_num or f"{r.owner}|{r.filed}|{r.doc_type}"
        if key not in seen:
            seen.add(key)
            unique.append(r)
    records = unique

    if not args.skip_parcel:
        enrich_parcels(records)
    detect_changes(records)
    score_records(records, start)
    records.sort(key=lambda r: (r.status != "NEW", -r.score))
    if not records:
        log.warning("No records found. Writing empty output files.")
    else:
        log.info("Total after dedup + enrichment: %d", len(records))
    write_outputs(records, start, end)
    pro_count = sum(1 for r in records if r.cat == "PRO")
    pro_with_addr = sum(1 for r in records if r.cat == "PRO" and r.prop_address)
    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("  Total records  : %d", len(records))
    log.info("  With address   : %d", sum(1 for r in records if r.prop_address))
    log.info("  Probates       : %d (%d with address)", pro_count, pro_with_addr)
    log.info("  Score >= 70    : %d", sum(1 for r in records if r.score >= 70))
    log.info("  Score >= 50    : %d", sum(1 for r in records if r.score >= 50))


if __name__ == "__main__":
    main()
