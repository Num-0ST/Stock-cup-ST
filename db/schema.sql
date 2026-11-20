-- =====================================================================
-- Multi-Warehouse Auto Inventory Management System
-- PostgreSQL 14+  |  24 tables  |  Phase 1 schema (for review)
-- Base unit = PC. All stock quantities are stored in base units (BIGINT).
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- USER MANAGEMENT (3)
-- ---------------------------------------------------------------------
CREATE TABLE roles (
    role_id      SMALLSERIAL PRIMARY KEY,
    role_code    VARCHAR(30)  NOT NULL UNIQUE,      -- ADMIN, MANAGER, STOREKEEPER, SALES, VIEWER
    role_name    VARCHAR(100) NOT NULL,
    permissions  JSONB        NOT NULL DEFAULT '[]'::jsonb  -- e.g. ["stock.receive","import.confirm"]
);

CREATE TABLE users (
    user_id        BIGSERIAL PRIMARY KEY,
    username       VARCHAR(50)  NOT NULL UNIQUE,
    password_hash  TEXT         NOT NULL,
    full_name      VARCHAR(150) NOT NULL,
    email          VARCHAR(150) UNIQUE,
    role_id        SMALLINT     NOT NULL REFERENCES roles(role_id),
    is_active      BOOLEAN      NOT NULL DEFAULT TRUE,
    last_login_at  TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- MASTER DATA (7)
-- ---------------------------------------------------------------------
CREATE TABLE categories (
    category_id  SERIAL PRIMARY KEY,
    parent_id    INT REFERENCES categories(category_id),
    name         VARCHAR(150) NOT NULL,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (parent_id, name)
);

CREATE TABLE units (
    unit_id    SMALLSERIAL PRIMARY KEY,
    unit_code  VARCHAR(10) NOT NULL UNIQUE,          -- CT, PK, PC
    unit_name  VARCHAR(50) NOT NULL,
    is_base    BOOLEAN NOT NULL DEFAULT FALSE
);
-- exactly one base unit (PC)
CREATE UNIQUE INDEX uq_units_single_base ON units (is_base) WHERE is_base;

CREATE TABLE products (
    product_id    BIGSERIAL PRIMARY KEY,
    product_code  VARCHAR(50)  NOT NULL UNIQUE,
    barcode       VARCHAR(64),                        -- base-unit barcode
    name          VARCHAR(255) NOT NULL,
    category_id   INT REFERENCES categories(category_id),
    base_unit_id  SMALLINT NOT NULL REFERENCES units(unit_id),
    reorder_level BIGINT NOT NULL DEFAULT 0 CHECK (reorder_level >= 0),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_products_barcode ON products (barcode) WHERE barcode IS NOT NULL;
CREATE INDEX ix_products_category ON products (category_id);
CREATE INDEX ix_products_name ON products (lower(name));

CREATE TABLE product_unit_conversions (
    conversion_id  BIGSERIAL PRIMARY KEY,
    product_id     BIGINT   NOT NULL REFERENCES products(product_id),
    unit_id        SMALLINT NOT NULL REFERENCES units(unit_id),
    factor_to_base INT      NOT NULL CHECK (factor_to_base > 0),  -- 1 CT = 24 PC -> 24
    barcode        VARCHAR(64),                                   -- optional per-unit barcode
    UNIQUE (product_id, unit_id)
);
CREATE UNIQUE INDEX uq_puc_barcode ON product_unit_conversions (barcode) WHERE barcode IS NOT NULL;

CREATE TABLE warehouses (
    warehouse_id    SERIAL PRIMARY KEY,
    warehouse_code  VARCHAR(20)  NOT NULL UNIQUE,
    name            VARCHAR(150) NOT NULL,
    warehouse_type  VARCHAR(10)  NOT NULL DEFAULT 'PHYSICAL'
                    CHECK (warehouse_type IN ('PHYSICAL','TRANSIT')),  -- TRANSIT = virtual in-transit account
    is_active       BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE customers (
    customer_id    BIGSERIAL PRIMARY KEY,
    customer_code  VARCHAR(30)  NOT NULL UNIQUE,
    name           VARCHAR(200) NOT NULL,
    phone          VARCHAR(30),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE suppliers (
    supplier_id    BIGSERIAL PRIMARY KEY,
    supplier_code  VARCHAR(30)  NOT NULL UNIQUE,
    name           VARCHAR(200) NOT NULL,
    phone          VARCHAR(30),
    is_active      BOOLEAN NOT NULL DEFAULT TRUE
);

-- user <-> warehouse access (depends on warehouses)
CREATE TABLE user_warehouse_access (
    user_id       BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    warehouse_id  INT    NOT NULL REFERENCES warehouses(warehouse_id),
    can_view      BOOLEAN NOT NULL DEFAULT TRUE,
    can_transact  BOOLEAN NOT NULL DEFAULT FALSE,
    can_approve   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (user_id, warehouse_id)
);

-- ---------------------------------------------------------------------
-- EXCEL IMPORT: templates / mappings first (batches reference templates)
-- ---------------------------------------------------------------------
CREATE TABLE import_templates (
    template_id    BIGSERIAL PRIMARY KEY,
    name           VARCHAR(150) NOT NULL UNIQUE,      -- e.g. "Shopee sales", "POS export"
    doc_type       VARCHAR(20)  NOT NULL CHECK (doc_type IN ('SALE','WITHDRAWAL','RECEIVING')),
    sheet_name     VARCHAR(100),
    header_row     INT NOT NULL DEFAULT 1 CHECK (header_row >= 1),
    match_by       VARCHAR(20) NOT NULL DEFAULT 'PRODUCT_CODE'
                   CHECK (match_by IN ('PRODUCT_CODE','BARCODE','BOTH')),
    settings       JSONB NOT NULL DEFAULT '{}'::jsonb,   -- default unit, doc-type rules (CN->return), warehouse map, ...
    created_by     BIGINT REFERENCES users(user_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE import_mappings (
    mapping_id     BIGSERIAL PRIMARY KEY,
    template_id    BIGINT NOT NULL REFERENCES import_templates(template_id) ON DELETE CASCADE,
    target_field   VARCHAR(40) NOT NULL,   -- doc_no, doc_date, product_code, barcode, qty, unit_code, customer_code ...
    source_column  VARCHAR(150) NOT NULL,  -- header text (or column letter) in the Excel file
    UNIQUE (template_id, target_field)
);

-- ---------------------------------------------------------------------
-- STOCK CORE: documents
-- ---------------------------------------------------------------------
CREATE TABLE import_batches (
    batch_id       BIGSERIAL PRIMARY KEY,
    template_id    BIGINT REFERENCES import_templates(template_id),
    warehouse_id   INT    NOT NULL REFERENCES warehouses(warehouse_id),  -- chosen BEFORE import
    doc_type       VARCHAR(20) NOT NULL CHECK (doc_type IN ('SALE','WITHDRAWAL','RECEIVING')),
    file_name      VARCHAR(255) NOT NULL,
    file_hash      CHAR(64)     NOT NULL,           -- SHA-256, detects re-upload of same file
    sheet_name     VARCHAR(100),
    header_row     INT NOT NULL DEFAULT 1,
    status         VARCHAR(15) NOT NULL DEFAULT 'UPLOADED'
                   CHECK (status IN ('UPLOADED','VALIDATED','CONFIRMED','CANCELLED','FAILED')),
    total_rows     INT NOT NULL DEFAULT 0,
    valid_rows     INT NOT NULL DEFAULT 0,
    error_rows     INT NOT NULL DEFAULT 0,
    duplicate_rows INT NOT NULL DEFAULT 0,
    options        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- mapping, headers, import options chosen by the user
    summary        JSONB NOT NULL DEFAULT '{}'::jsonb,   -- result of the last validation (docs ready, shortages, ...)
    uploaded_by    BIGINT NOT NULL REFERENCES users(user_id),
    uploaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_by   BIGINT REFERENCES users(user_id),
    confirmed_at   TIMESTAMPTZ
);
CREATE INDEX ix_import_batches_wh_status ON import_batches (warehouse_id, status);
CREATE INDEX ix_import_batches_hash ON import_batches (warehouse_id, file_hash);

CREATE TABLE stock_documents (
    document_id    BIGSERIAL PRIMARY KEY,
    doc_type       VARCHAR(20) NOT NULL
                   CHECK (doc_type IN ('SALE','WITHDRAWAL','RECEIVING','CUSTOMER_RETURN','SUPPLIER_RETURN','ADJUSTMENT')),
    doc_no         VARCHAR(50) NOT NULL,
    doc_date       DATE NOT NULL,
    warehouse_id   INT  NOT NULL REFERENCES warehouses(warehouse_id),
    customer_id    BIGINT REFERENCES customers(customer_id),
    supplier_id    BIGINT REFERENCES suppliers(supplier_id),
    status         VARCHAR(12) NOT NULL DEFAULT 'DRAFT'
                   CHECK (status IN ('DRAFT','POSTED','REVERSED')),
    source         VARCHAR(10) NOT NULL DEFAULT 'MANUAL' CHECK (source IN ('MANUAL','IMPORT')),
    import_batch_id BIGINT REFERENCES import_batches(batch_id),
    reverses_document_id BIGINT REFERENCES stock_documents(document_id),
    remark         TEXT,
    created_by     BIGINT NOT NULL REFERENCES users(user_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    posted_by      BIGINT REFERENCES users(user_id),
    posted_at      TIMESTAMPTZ,
    -- duplicate protection: same doc number cannot be posted twice for a type/warehouse
    UNIQUE (doc_type, warehouse_id, doc_no)
);
CREATE INDEX ix_stock_documents_date ON stock_documents (doc_date, doc_type);
CREATE INDEX ix_stock_documents_batch ON stock_documents (import_batch_id);

CREATE TABLE stock_document_items (
    item_id        BIGSERIAL PRIMARY KEY,
    document_id    BIGINT NOT NULL REFERENCES stock_documents(document_id) ON DELETE CASCADE,
    line_no        INT    NOT NULL,
    product_id     BIGINT NOT NULL REFERENCES products(product_id),
    unit_id        SMALLINT NOT NULL REFERENCES units(unit_id),
    qty            NUMERIC(18,3) NOT NULL CHECK (qty > 0),        -- qty in the unit used on the document
    conversion_factor INT NOT NULL CHECK (conversion_factor > 0), -- SNAPSHOT of rate at document time
    base_qty       BIGINT NOT NULL CHECK (base_qty > 0),          -- qty * factor, in PC
    UNIQUE (document_id, line_no)
);
CREATE INDEX ix_sdi_product ON stock_document_items (product_id);

-- ---------------------------------------------------------------------
-- STOCK CORE: ledger + balances
-- ---------------------------------------------------------------------
CREATE TABLE stock_balances (
    warehouse_id  INT    NOT NULL REFERENCES warehouses(warehouse_id),
    product_id    BIGINT NOT NULL REFERENCES products(product_id),
    qty_on_hand   BIGINT NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (warehouse_id, product_id)
    -- negative stock is prevented for PHYSICAL warehouses by the trigger below
);
CREATE INDEX ix_stock_balances_product ON stock_balances (product_id);

CREATE TABLE stock_transactions (
    txn_id          BIGSERIAL PRIMARY KEY,                -- append-only ledger
    txn_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    txn_date        DATE NOT NULL,                        -- business date
    warehouse_id    INT    NOT NULL REFERENCES warehouses(warehouse_id),
    product_id      BIGINT NOT NULL REFERENCES products(product_id),
    txn_type        VARCHAR(25) NOT NULL
                    CHECK (txn_type IN ('RECEIVING','SALE','WITHDRAWAL','CUSTOMER_RETURN','SUPPLIER_RETURN',
                                        'ADJUST_IN','ADJUST_OUT','TRANSFER_OUT','TRANSFER_IN',
                                        'TRANSIT_IN','TRANSIT_OUT','REVERSAL')),
    qty_in          BIGINT NOT NULL DEFAULT 0 CHECK (qty_in  >= 0),
    qty_out         BIGINT NOT NULL DEFAULT 0 CHECK (qty_out >= 0),
    balance_after   BIGINT NOT NULL,                      -- running balance in this warehouse
    document_id     BIGINT REFERENCES stock_documents(document_id),
    document_item_id BIGINT REFERENCES stock_document_items(item_id),
    transfer_id     BIGINT,                               -- FK added after stock_transfers is created
    adjustment_id   BIGINT,                               -- FK added after stock_adjustments is created
    reverses_txn_id BIGINT REFERENCES stock_transactions(txn_id),
    created_by      BIGINT NOT NULL REFERENCES users(user_id),
    CHECK ((qty_in > 0 AND qty_out = 0) OR (qty_in = 0 AND qty_out > 0))
);
CREATE INDEX ix_st_product_wh_date ON stock_transactions (product_id, warehouse_id, txn_date, txn_id);
CREATE INDEX ix_st_wh_date        ON stock_transactions (warehouse_id, txn_date);
CREATE INDEX ix_st_document       ON stock_transactions (document_id);
-- Double-deduction protection: a document line posts at most once; a txn is reversed at most once
CREATE UNIQUE INDEX uq_st_item_once ON stock_transactions (document_item_id) WHERE document_item_id IS NOT NULL AND reverses_txn_id IS NULL;
CREATE UNIQUE INDEX uq_st_reversed_once ON stock_transactions (reverses_txn_id) WHERE reverses_txn_id IS NOT NULL;

CREATE TABLE stock_counts (                               -- physical count sheets
    count_id      BIGSERIAL PRIMARY KEY,
    count_no      VARCHAR(50) NOT NULL UNIQUE,
    warehouse_id  INT    NOT NULL REFERENCES warehouses(warehouse_id),
    product_id    BIGINT NOT NULL REFERENCES products(product_id),
    count_date    DATE   NOT NULL,
    system_qty    BIGINT NOT NULL,                        -- snapshot of qty_on_hand at count time
    counted_qty   BIGINT NOT NULL CHECK (counted_qty >= 0),
    status        VARCHAR(10) NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','ADJUSTED','CANCELLED')),
    counted_by    BIGINT NOT NULL REFERENCES users(user_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_stock_counts_wh_product ON stock_counts (warehouse_id, product_id);

CREATE TABLE stock_adjustments (
    adjustment_id BIGSERIAL PRIMARY KEY,
    adjust_no     VARCHAR(50) NOT NULL UNIQUE,
    count_id      BIGINT REFERENCES stock_counts(count_id),
    warehouse_id  INT    NOT NULL REFERENCES warehouses(warehouse_id),
    product_id    BIGINT NOT NULL REFERENCES products(product_id),
    diff_qty      BIGINT NOT NULL CHECK (diff_qty <> 0),  -- +increase / -decrease (base units)
    reason        TEXT,
    status        VARCHAR(10) NOT NULL DEFAULT 'POSTED' CHECK (status IN ('POSTED','REVERSED')),
    created_by    BIGINT NOT NULL REFERENCES users(user_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_adjustment_per_count ON stock_adjustments (count_id) WHERE count_id IS NOT NULL AND status = 'POSTED';

-- ---------------------------------------------------------------------
-- STOCK TRANSFER (2)
-- ---------------------------------------------------------------------
CREATE TABLE stock_transfers (
    transfer_id       BIGSERIAL PRIMARY KEY,
    transfer_no       VARCHAR(50) NOT NULL UNIQUE,
    transfer_mode     VARCHAR(10) NOT NULL CHECK (transfer_mode IN ('INSTANT','APPROVAL')),
    from_warehouse_id INT NOT NULL REFERENCES warehouses(warehouse_id),
    to_warehouse_id   INT NOT NULL REFERENCES warehouses(warehouse_id),
    transit_warehouse_id INT REFERENCES warehouses(warehouse_id),  -- virtual TRANSIT warehouse (APPROVAL mode)
    status            VARCHAR(15) NOT NULL DEFAULT 'DRAFT'
                      CHECK (status IN ('DRAFT','IN_TRANSIT','PARTIAL','COMPLETED','CANCELLED')),
    transfer_date     DATE NOT NULL,
    remark            TEXT,
    created_by        BIGINT NOT NULL REFERENCES users(user_id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    shipped_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ,
    cancelled_by      BIGINT REFERENCES users(user_id),
    cancelled_at      TIMESTAMPTZ,
    CHECK (from_warehouse_id <> to_warehouse_id)
);
CREATE INDEX ix_transfers_status ON stock_transfers (status, transfer_date);

CREATE TABLE stock_transfer_items (
    item_id         BIGSERIAL PRIMARY KEY,
    transfer_id     BIGINT NOT NULL REFERENCES stock_transfers(transfer_id) ON DELETE CASCADE,
    product_id      BIGINT NOT NULL REFERENCES products(product_id),
    unit_id         SMALLINT NOT NULL REFERENCES units(unit_id),
    conversion_factor INT NOT NULL CHECK (conversion_factor > 0),
    qty_sent_base       BIGINT NOT NULL CHECK (qty_sent_base > 0),
    qty_received_base   BIGINT NOT NULL DEFAULT 0 CHECK (qty_received_base >= 0),
    qty_cancelled_base  BIGINT NOT NULL DEFAULT 0 CHECK (qty_cancelled_base >= 0),  -- returned to source
    UNIQUE (transfer_id, product_id),
    CHECK (qty_received_base + qty_cancelled_base <= qty_sent_base)
);

-- deferred FKs from the ledger
ALTER TABLE stock_transactions
    ADD CONSTRAINT fk_st_transfer   FOREIGN KEY (transfer_id)   REFERENCES stock_transfers(transfer_id),
    ADD CONSTRAINT fk_st_adjustment FOREIGN KEY (adjustment_id) REFERENCES stock_adjustments(adjustment_id);
CREATE INDEX ix_st_transfer ON stock_transactions (transfer_id);

-- ---------------------------------------------------------------------
-- EXCEL IMPORT: rows
-- ---------------------------------------------------------------------
CREATE TABLE import_rows (
    row_id        BIGSERIAL PRIMARY KEY,
    batch_id      BIGINT NOT NULL REFERENCES import_batches(batch_id) ON DELETE CASCADE,
    row_no        INT    NOT NULL,                        -- Excel row number
    raw_data      JSONB  NOT NULL,                        -- original cells, for error export
    doc_no        VARCHAR(50),
    doc_date      DATE,
    doc_type      VARCHAR(20),                            -- decided per row (e.g. SALE, or CUSTOMER_RETURN for CN)
    warehouse_id  INT REFERENCES warehouses(warehouse_id),
    product_ref   VARCHAR(100),                           -- code/barcode text exactly as in the file
    product_id    BIGINT REFERENCES products(product_id), -- resolved by code/barcode
    unit_code     VARCHAR(10),
    unit_id       SMALLINT REFERENCES units(unit_id),
    qty           NUMERIC(18,3),                          -- as in the file (signed)
    base_qty      BIGINT,                                 -- positive, in PC
    customer_code VARCHAR(50),
    customer_name VARCHAR(200),
    salesman      VARCHAR(50),
    status        VARCHAR(10) NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING','VALID','ERROR','DUPLICATE','BLOCKED','POSTED')),
    error_code    VARCHAR(40),                            -- PRODUCT_NOT_FOUND, BAD_QTY, UNIT_NOT_DEFINED, DUP_IN_FILE, DUP_IN_DB, INSUFFICIENT_STOCK ...
    error_message TEXT,
    row_hash      CHAR(64),                               -- dedupe within/between files
    document_id   BIGINT REFERENCES stock_documents(document_id),
    UNIQUE (batch_id, row_no)
);
CREATE INDEX ix_import_rows_batch_status ON import_rows (batch_id, status);
CREATE INDEX ix_import_rows_hash ON import_rows (row_hash);

-- ---------------------------------------------------------------------
-- AUDIT (2)
-- ---------------------------------------------------------------------
CREATE TABLE audit_logs (
    audit_id     BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id      BIGINT REFERENCES users(user_id),
    action       VARCHAR(50) NOT NULL,                    -- LOGIN, POST_DOC, REVERSE_DOC, IMPORT_CONFIRM, TRANSFER_SHIP ...
    entity_type  VARCHAR(50),
    entity_id    VARCHAR(50),
    warehouse_id INT REFERENCES warehouses(warehouse_id),
    old_data     JSONB,
    new_data     JSONB,
    ip_address   INET
);
CREATE INDEX ix_audit_entity ON audit_logs (entity_type, entity_id);
CREATE INDEX ix_audit_user_time ON audit_logs (user_id, occurred_at DESC);

CREATE TABLE idempotency_keys (
    idem_key      VARCHAR(100) PRIMARY KEY,               -- client-generated per action (confirm import, post doc, ...)
    user_id       BIGINT NOT NULL REFERENCES users(user_id),
    action        VARCHAR(50) NOT NULL,
    request_hash  CHAR(64) NOT NULL,
    status        VARCHAR(12) NOT NULL DEFAULT 'IN_PROGRESS' CHECK (status IN ('IN_PROGRESS','DONE','FAILED')),
    response_ref  VARCHAR(100),                           -- e.g. document_id / batch_id of the result
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL DEFAULT now() + interval '7 days'
);

-- ---------------------------------------------------------------------
-- SAFETY TRIGGERS
-- ---------------------------------------------------------------------
-- 1) No negative stock in PHYSICAL warehouses
CREATE OR REPLACE FUNCTION trg_no_negative_stock() RETURNS trigger AS $$
BEGIN
    IF NEW.qty_on_hand < 0 THEN
        RAISE EXCEPTION 'INSUFFICIENT_STOCK: warehouse=% product=% would become %',
            NEW.warehouse_id, NEW.product_id, NEW.qty_on_hand USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stock_balances_no_negative
    BEFORE INSERT OR UPDATE ON stock_balances
    FOR EACH ROW EXECUTE FUNCTION trg_no_negative_stock();

-- 2) Ledger is append-only
CREATE OR REPLACE FUNCTION trg_ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'stock_transactions is append-only; post a REVERSAL instead';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER stock_transactions_no_update
    BEFORE UPDATE OR DELETE ON stock_transactions
    FOR EACH ROW EXECUTE FUNCTION trg_ledger_append_only();

-- ---------------------------------------------------------------------
-- SEED: required master rows
-- ---------------------------------------------------------------------
INSERT INTO units (unit_code, unit_name, is_base) VALUES
    ('PC','Piece',TRUE), ('PK','Pack',FALSE), ('CT','Carton',FALSE);

INSERT INTO roles (role_code, role_name) VALUES
    ('ADMIN','ผู้ดูแลระบบ'), ('MANAGER','ผู้จัดการ'), ('STOREKEEPER','พนักงานคลัง'),
    ('SALES','ฝ่ายขาย'), ('VIEWER','ดูอย่างเดียว');

INSERT INTO warehouses (warehouse_code, name, warehouse_type) VALUES
    ('TRANSIT','คลังระหว่างทาง (Virtual)','TRANSIT');

COMMIT;
