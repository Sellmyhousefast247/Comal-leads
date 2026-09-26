# Comal-leads

Motivated-seller lead scraper for **Comal County, TX**. Cloned from the
Bexar/Dallas county scraper systems.

**Live dashboard:** https://sellmyhousefast247.github.io/Comal-leads/

## Sources
- **Tyler Odyssey Public Access** (anonymous, no login):
  http://public.co.comal.tx.us/
  - Civil/Family (7-day): Tax Cases → tax foreclosure, Real Property →
    lis pendens-equivalent, Suits on Debt + Debt Claim → judgments
  - Probate (60-day): PC-prefixed estate cases; decedent parsed from
    "ESTATE OF ..." style
- **Monthly foreclosure posting PDF** (scanned) from
  https://www.comalcounty.gov/213/Foreclosure-Sales — OCR'd in CI with
  tesseract; best-effort address/mortgagor/sale-date per notice → cat FC
- **Comal CAD parcels** (BIS webmap FeatureServer, ~106,800 parcels) —
  owner-forward and address-reverse enrichment (situs + mailing +
  market value)

## Pipeline
County → scrape → normalize → hash/dedupe → NEW/CHANGED detection →
score → export (`dashboard/records.json`, `data/ghl_export.csv`,
`data/skiptrace_export.csv`). State in `data/state.json`.

## Runs
Daily via GitHub Actions (13:00 UTC) + manual `workflow_dispatch`.

## Comal-specific notes
- The county recorder is **ROAM** (comal.landrecordsonline.com) which
  requires an account even for its free $0 Pay-As-You-Go tier, so the
  recorder index (lis pendens, liens, heirship affidavits, trustee
  notices) is NOT scraped yet. **Upgrade path:** register the free ROAM
  account, add `ROAM_USER` / `ROAM_PASS` repo secrets, and extend
  fetch.py with a ROAM login + search pass.
- CAD eSearch (esearch.comalad.org) is reCAPTCHA-protected — not
  automatable; the BIS webmap parcel FeatureServer is used instead
  (requires a Referer header).
- Odyssey Search.aspx must be reached by clicking the launch link on
  default.aspx (server session); deep links redirect back.
- Foreclosure PDFs are scanned images; OCR quality varies, so FC
  records may carry partial addresses. Address-reverse CAD lookup fills
  owners where the OCR address is clean.
