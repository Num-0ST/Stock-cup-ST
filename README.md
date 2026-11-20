# Multi-Warehouse Inventory — Phases 1–3 (DB, Stock Engine, Excel Import)

Stack: Python 3.13 / FastAPI · PostgreSQL 16 · plain-JS web UI (Thai / English / Lao).

## Deploy (server or cloud, Docker)
```bash
cp .env.example .env        # fill DB_PASSWORD, SECRET_KEY (>=32 chars), ADMIN_PASSWORD (>=8 chars)
docker compose up -d --build
# open http://<server>:8000  ->  user "admin" / ADMIN_PASSWORD
```
Put it behind HTTPS (reverse proxy such as Caddy/nginx) before exposing it to the internet.
The schema in `db/schema.sql` is applied automatically the first time the database volume is created.
Back up with: `docker compose exec db pg_dump -U wms wms > backup.sql`

## Tests
```bash
cd backend
export DATABASE_URL=postgresql://user@host/wms_test   # a THROWAWAY database: tests DROP and recreate its schema
pytest tests -q
```

## What exists in Phase 2
Login + roles + per-warehouse access · master data (products with CT/PK/PC conversion, warehouses, customers,
suppliers, users) · receiving / sales / withdrawal / customer return / supplier return documents (draft → post) ·
reversal · stock count + adjustment · stock balance (per warehouse and total) · stock movement report ·
audit log · idempotency keys.
Not yet built: Excel import (Phase 3), transfers (Phase 4), dashboard / reports / PDF-Excel export (Phase 5).


## Phase 3 — Excel import (menu: Import Excel)
Upload `.xlsx` / `.xls` (including HTML/PivotTable-style exports such as `Export_28.xls`) / `.csv`, then:
1. **Upload** – choose warehouse + document type (SALE / WITHDRAWAL / RECEIVING).
2. **Map columns** – header row and columns are auto-detected; set defaults (unit, doc no., date), prefix rules
   (e.g. `CN` → customer return, which *increases* stock), and save the setup as a template.
3. **Check** – validation only, **no stock changes**. Per-row errors, duplicates (already in system), shortages
   (chronological simulation), optional salesman → warehouse mapping, error file download.
4. **Confirm** – one transaction: documents are posted in date order under row locks; any failure rolls everything back.
   Re-importing the same file creates no duplicates. A document with one bad row is blocked as a whole.

Uploaded files are kept in `UPLOAD_DIR` (volume `uploads` in docker-compose). Opening stock can be imported as RECEIVING
with a default document no. and date.

## Phase 4 — Transfers (menu: Transfers)
- **Instant**: source OUT + destination IN in one transaction, status COMPLETED (needs transact right on both warehouses).
- **Needs approval**: DRAFT → manager *Approve & ship* (source OUT, virtual TRANSIT IN) → IN_TRANSIT →
  destination *Receive* (full or partial; PARTIAL until nothing is outstanding) → COMPLETED.
- **Cancel**: DRAFT cancels without stock effect; IN_TRANSIT/PARTIAL returns the outstanding quantity to the source.
- Every step locks the transfer row then balances in (warehouse, product) order, so double-click / concurrent requests
  apply once and opposite transfers cannot deadlock. All movements appear in the ledger and Stock movement page.
