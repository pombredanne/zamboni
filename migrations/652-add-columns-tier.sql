ALTER TABLE price_currency ADD COLUMN dev bool NOT NULL DEFAULT TRUE;
ALTER TABLE price_currency ADD COLUMN paid bool NOT NULL DEFAULT TRUE;