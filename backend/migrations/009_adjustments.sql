-- Stock adjustment documents (ADJUSTMENT): each line raises (+1) or lowers (-1) the stock, with a reason.
ALTER TABLE stock_document_items ADD COLUMN IF NOT EXISTS adj_sign SMALLINT;
ALTER TABLE stock_documents ADD COLUMN IF NOT EXISTS adjust_reason VARCHAR(20);
DO $$ BEGIN
    ALTER TABLE stock_document_items ADD CONSTRAINT ck_sdi_adj_sign CHECK (adj_sign IN (-1, 1));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE stock_documents ADD CONSTRAINT ck_sd_adjust_reason
        CHECK (adjust_reason IN ('DAMAGED','EXPIRED','LOST','FOUND','RECOUNT','SAMPLE','OTHER'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
