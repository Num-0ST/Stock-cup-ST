-- Keep reports fast as the ledger grows over the years.
-- reversals are few: a small partial index finds them by date
CREATE INDEX IF NOT EXISTS ix_st_reversal_date ON stock_transactions (txn_date) WHERE reverses_txn_id IS NOT NULL;
-- sales / returns per warehouse and day, read straight from the index (no table visits)
CREATE INDEX IF NOT EXISTS ix_st_wh_date_cover ON stock_transactions (warehouse_id, txn_date)
    INCLUDE (txn_type, product_id, qty_in, qty_out, document_id);
DROP INDEX IF EXISTS ix_st_wh_date;
