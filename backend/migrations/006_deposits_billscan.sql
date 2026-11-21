-- Customer deposits (sold goods kept in the warehouse until delivery) + bill scanning.
-- Idempotent: safe on a fresh database (schema.sql already has all of this) and on upgrades.

ALTER TABLE stock_document_items ADD COLUMN IF NOT EXISTS deposit_qty_base BIGINT NOT NULL DEFAULT 0;
DO $$ BEGIN
    ALTER TABLE stock_document_items ADD CONSTRAINT ck_sdi_deposit_qty
        CHECK (deposit_qty_base >= 0 AND deposit_qty_base <= base_qty);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE stock_counts ADD COLUMN IF NOT EXISTS deposit_qty BIGINT NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS customer_deposits (
    deposit_id        BIGSERIAL PRIMARY KEY,
    deposit_no        VARCHAR(50) NOT NULL UNIQUE,
    sale_document_id  BIGINT NOT NULL REFERENCES stock_documents(document_id),
    warehouse_id      INT    NOT NULL REFERENCES warehouses(warehouse_id),
    customer_id       BIGINT REFERENCES customers(customer_id),
    customer_name     VARCHAR(200),
    deposit_date      DATE   NOT NULL,
    due_date          DATE,
    status            VARCHAR(12) NOT NULL DEFAULT 'OPEN'
                      CHECK (status IN ('OPEN','PARTIAL','COMPLETED','CANCELLED')),
    remark            TEXT,
    created_by        BIGINT NOT NULL REFERENCES users(user_id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at         TIMESTAMPTZ,
    cancelled_by      BIGINT REFERENCES users(user_id),
    cancel_reason     TEXT,
    return_document_id BIGINT REFERENCES stock_documents(document_id)
);
CREATE INDEX IF NOT EXISTS ix_deposits_status ON customer_deposits (status, warehouse_id);
CREATE INDEX IF NOT EXISTS ix_deposits_sale ON customer_deposits (sale_document_id);
CREATE INDEX IF NOT EXISTS ix_deposits_customer ON customer_deposits (customer_id);

CREATE TABLE IF NOT EXISTS customer_deposit_items (
    deposit_item_id     BIGSERIAL PRIMARY KEY,
    deposit_id          BIGINT NOT NULL REFERENCES customer_deposits(deposit_id) ON DELETE CASCADE,
    sale_item_id        BIGINT NOT NULL REFERENCES stock_document_items(item_id),
    product_id          BIGINT NOT NULL REFERENCES products(product_id),
    unit_id             SMALLINT NOT NULL REFERENCES units(unit_id),
    conversion_factor   INT    NOT NULL CHECK (conversion_factor > 0),
    qty_base            BIGINT NOT NULL CHECK (qty_base > 0),
    qty_delivered_base  BIGINT NOT NULL DEFAULT 0 CHECK (qty_delivered_base >= 0),
    qty_cancelled_base  BIGINT NOT NULL DEFAULT 0 CHECK (qty_cancelled_base >= 0),
    UNIQUE (deposit_id, sale_item_id),
    CHECK (qty_delivered_base + qty_cancelled_base <= qty_base)
);
CREATE INDEX IF NOT EXISTS ix_cdi_product ON customer_deposit_items (product_id);
CREATE INDEX IF NOT EXISTS ix_cdi_sale_item ON customer_deposit_items (sale_item_id);

CREATE TABLE IF NOT EXISTS deposit_deliveries (
    delivery_id    BIGSERIAL PRIMARY KEY,
    delivery_no    VARCHAR(50) NOT NULL UNIQUE,
    deposit_id     BIGINT NOT NULL REFERENCES customer_deposits(deposit_id),
    delivery_date  DATE NOT NULL,
    remark         TEXT,
    created_by     BIGINT NOT NULL REFERENCES users(user_id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_deliveries_deposit ON deposit_deliveries (deposit_id);

CREATE TABLE IF NOT EXISTS deposit_delivery_items (
    delivery_id      BIGINT NOT NULL REFERENCES deposit_deliveries(delivery_id) ON DELETE CASCADE,
    deposit_item_id  BIGINT NOT NULL REFERENCES customer_deposit_items(deposit_item_id),
    qty_base         BIGINT NOT NULL CHECK (qty_base > 0),
    PRIMARY KEY (delivery_id, deposit_item_id)
);

CREATE TABLE IF NOT EXISTS bill_scans (
    scan_id       BIGSERIAL PRIMARY KEY,
    file_path     TEXT   NOT NULL,
    file_sha256   CHAR(64) NOT NULL,
    mime_type     VARCHAR(40) NOT NULL,
    ai_model      VARCHAR(60),
    ai_result     JSONB  NOT NULL DEFAULT '{}'::jsonb,
    status        VARCHAR(10) NOT NULL DEFAULT 'READ' CHECK (status IN ('READ','FAILED','USED')),
    error_message TEXT,
    document_id   BIGINT REFERENCES stock_documents(document_id),
    created_by    BIGINT NOT NULL REFERENCES users(user_id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_bill_scans_sha ON bill_scans (file_sha256);
CREATE INDEX IF NOT EXISTS ix_bill_scans_doc ON bill_scans (document_id);
