CREATE OR REPLACE FUNCTION drop_all_indexes() RETURNS INTEGER AS $$
DECLARE
  i RECORD;
BEGIN
  FOR i IN 
    (SELECT relname FROM pg_class
       -- exclude all pkey, exclude system catalog which starts with 'pg_'
      WHERE relkind = 'i' AND relname NOT LIKE '%_pkey%' AND relname NOT LIKE 'pg_%')
  LOOP
    -- RAISE INFO 'DROPING INDEX: %', i.relname;
    EXECUTE 'DROP INDEX ' || i.relname;
  END LOOP;
RETURN 1;
END;
$$ LANGUAGE plpgsql;