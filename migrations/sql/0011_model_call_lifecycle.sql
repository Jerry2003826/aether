-- A reported provider cost may exceed the reservation. It is stored as observed,
-- not erased to NULL and not settled as if it fit.

DO $$
DECLARE
  cons text;
BEGIN
  FOR cons IN
    SELECT conname FROM pg_constraint
    WHERE conrelid = 'model_requests'::regclass
      AND contype = 'c'
      AND pg_get_constraintdef(oid) LIKE '%usage_status%'
  LOOP
    EXECUTE format('ALTER TABLE model_requests DROP CONSTRAINT %I', cons);
  END LOOP;
END $$;

ALTER TABLE model_requests ADD CONSTRAINT model_requests_usage_status_check
  CHECK (usage_status IN ('reserved', 'known', 'unknown', 'scripted', 'cancelled_before_dispatch', 'observed'));

ALTER TABLE model_requests ADD CONSTRAINT model_requests_known_fields_check
  CHECK (usage_status <> 'known' OR (actual_micro_usd IS NOT NULL AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL));

ALTER TABLE model_requests ADD CONSTRAINT model_requests_unknown_cost_check
  CHECK (usage_status <> 'unknown' OR actual_micro_usd IS NULL);
