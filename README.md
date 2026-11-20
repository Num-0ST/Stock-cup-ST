# Multi-Warehouse Inventory — Phases 1–5

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

## Phase 5 — Dashboard, reports, security, backup
**Dashboard** (home page): stock totals, low / out-of-stock, today's and period net sales, in-transit transfers, daily sales and
in/out charts, top products, stock by warehouse, items to reorder. Filter by warehouse and 7/30/90 days.

**Reports** (screen preview + export to **Excel** and **PDF**, in Thai / English / Lao): stock balance, low stock, stock movements,
sales summary, fast / slow moving, transfers, audit log (admin). Everything is scoped to the warehouses the user may view.
- Sales are *net*: returns are subtracted and reversed documents are not counted.
- Fast/slow: ranked by net sales in the last N days — top 20 % of sellers = FAST, bottom 20 % = SLOW, products with stock but
  no sales = DEAD (needs at least 3 selling products to label any SLOW).
- PDF needs fonts on the server (installed by the Dockerfile: DejaVu + Thai Loma + Lao Phetsarath). Thai/Lao are shaped with HarfBuzz.

**System integrity check** (admin, Reports page / `GET /api/admin/integrity`): re-derives every balance from the ledger and checks
for negative stock, balances ≠ ledger, posted lines without ledger rows, in-transit stock ≠ open transfers, double reversals.

**Security**: account lock for 15 min after 5 wrong passwords (admin can unlock: `POST /api/users/{id}/unlock`); password policy
(≥ 8 chars, letters + digits); changing/resetting a password signs out all other sessions; failed/blocked logins are audited with IP;
security headers + CSP; API responses are `no-store`; every report export is audited.
Upgrading an existing database: `psql -f db/migrations/005_security.sql`.

**Backup / restore** (`scripts/`):
- `docker compose` runs a `backup` service: daily at `BACKUP_HOUR` (default 02:00) → `./backups/wms-<timestamp>.dump` (+ `.sha256`, + uploads tarball),
  verified after writing, old files removed after `BACKUP_KEEP_DAYS` (default 14). Copy `./backups` off the server (rsync / object storage).
- Manual backup: `scripts/backup.sh` (needs `PG*` env). Restore: stop the API, then `FORCE=1 scripts/restore.sh backups/<file>.dump wms`.
  The script verifies the checksum and refuses to overwrite a non-empty database unless `FORCE=1`.
- Restore was tested by dumping a live database and comparing all 24 tables row-by-row after restoring.
