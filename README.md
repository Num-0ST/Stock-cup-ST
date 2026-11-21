# Multi-Warehouse Inventory — Phases 1–6

Stack: Python 3.13 / FastAPI · PostgreSQL 16 · plain-JS web UI (Thai / English / Lao).

## Deploy (server or cloud, Docker)
```bash
cp .env.example .env        # fill DB_PASSWORD, SECRET_KEY (>=32 chars), ADMIN_PASSWORD (>=8 chars)
docker compose up -d --build
# open http://<server>:8000  ->  user "admin" / ADMIN_PASSWORD
```
Put it behind HTTPS (reverse proxy such as Caddy/nginx) before exposing it to the internet.
The schema in `db/schema.sql` is applied automatically the first time the database volume is created.
Upgrading: `git pull && docker compose up -d --build` — database changes in `backend/migrations/*.sql` are applied
automatically when the API starts (recorded in `schema_migrations`; every file is safe to run more than once).
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
Existing databases are upgraded automatically on startup (see Deploy).

**Backup / restore** (`scripts/`):
- `docker compose` runs a `backup` service: daily at `BACKUP_HOUR` (default 02:00) → `./backups/wms-<timestamp>.dump` (+ `.sha256`, + uploads tarball),
  verified after writing, old files removed after `BACKUP_KEEP_DAYS` (default 14). Copy `./backups` off the server (rsync / object storage).
- Manual backup: `scripts/backup.sh` (needs `PG*` env). Restore: stop the API, then `FORCE=1 scripts/restore.sh backups/<file>.dump wms`.
  The script verifies the checksum and refuses to overwrite a non-empty database unless `FORCE=1`.
- Restore was tested by dumping a live database and comparing all 24 tables row-by-row after restoring.

## Phase 6 — Bill scanning & customer deposits

**Scan bill (menu: สแกนบิล)** — take a photo of a sales bill; the system reads bill no., date (Buddhist years converted),
customer, salesman, lines, units and "ฝาก" quantities, matches them to products (code → product barcode → unit barcode →
name similarity), units (ลัง/แพ็ค/ชิ้น/CT/PK/PC …), customer and warehouse (salesman code = warehouse code, or the salesman
map saved in an import template), then opens an editable **draft** next to the photo with a warning on everything uncertain
(duplicate bill no., photo already used, unknown product/unit, name match). Nothing touches stock until the user confirms.
Two readers (`BILL_OCR_ENGINE`):
- **Free (default)** — Tesseract OCR on the server (installed by the Dockerfile; Thai + English, `BILL_OCR_LANGS=tha+lao+eng`
  for Lao). No cost, no internet. Two passes (Thai+English for names/units, English for codes/numbers), codes matched through
  look-alike folding (O/0, I/1, S/5 …) against the product / customer / warehouse codes already in the system. Good on printed
  bills; handwriting is read poorly. The recognised text is shown on the draft.
- **Claude AI (optional, paid)** — set `ANTHROPIC_API_KEY` (and keep `BILL_OCR_ENGINE=auto`, or `claude`): better on
  handwriting, roughly 0.1–0.5 THB per bill with `BILL_AI_MODEL=claude-sonnet-5-5`.
- **Barcode scanning**: on every new document there is a scan box — a USB/Bluetooth barcode scanner (types + Enter) or typing a
  code adds the product (+1 per scan; unit barcodes add that unit). The 📷 button reads a barcode/QR code from a phone photo
  (native BarcodeDetector, else bundled ZXing). The same button on the Documents page searches by bill number.

**Customer deposits (menu: ฝากคลัง)** — the customer paid but leaves part of the goods with us until a later delivery.
- Stock is deducted when the sale is posted, so deposited goods can never be sold again. The deposit tracks what we still owe.
- Create it on the sale itself (column "ฝากไว้" on each line) or later from a posted sale (button "ฝากคลัง"). A sale line can be
  deposited at most up to its sold quantity; a deposit needs a customer.
- Deliver in parts or all at once (delivery no. + history). Deliveries do not move stock (already deducted).
- Cancel the rest (manager): either the customer returns the goods → a CUSTOMER_RETURN is posted automatically, or just close it.
  Reversing the sale closes its deposits.
- Stock page shows *Available* (sellable), *Customer deposits* and *In warehouse* (= both). **Stock counts count everything on the
  shelf including deposits**; the system subtracts open deposits before adjusting.
- Report "ของฝากลูกค้า" (Excel/PDF), dashboard card, and 4 new integrity checks.

### Document lines from Excel
On every new document (e.g. an opening-balance receipt with 200-300 items) the button **นำเข้ารายการจาก Excel** reads an
.xlsx / .xls / .csv file and fills the lines: product by code, barcode, unit barcode or exact name; quantity; optional unit
(PC/PK/CT or ชิ้น/แพ็ค/ลัง), or separate ลัง / แพ็ค / ชิ้น columns (summed to pieces). Headers are found automatically
(Thai / Lao / English, title rows above are fine); without headers the columns are guessed. Duplicate codes are merged,
empty / 0 rows skipped, and rows that cannot be used (unknown product, bad quantity, unit not set up) are listed with their
row numbers. "ดาวน์โหลดแบบฟอร์ม" gives an .xlsx with every product code and name to fill in. Nothing is saved until the
user saves the document.
