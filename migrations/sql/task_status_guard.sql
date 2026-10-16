-- Terminal task statuses cannot become schedulable again. The API still cannot
-- advance a projection past DRAFT/QUEUED. Replacing the function updates the
-- existing BEFORE UPDATE trigger.

CREATE OR REPLACE FUNCTION aether_reject_api_task_projection() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('SUCCEEDED', 'FAILED', 'CANCELLED')
     AND NEW.status IS DISTINCT FROM OLD.status THEN
    RAISE EXCEPTION 'terminal task cannot return from % to %', OLD.status, NEW.status;
  END IF;
  IF current_user = 'aether_api'
     AND NEW.status IS DISTINCT FROM OLD.status
     AND NEW.status NOT IN ('DRAFT', 'QUEUED') THEN
    RAISE EXCEPTION 'api role cannot advance task projection to %', NEW.status;
  END IF;
  RETURN NEW;
END;
$$;
