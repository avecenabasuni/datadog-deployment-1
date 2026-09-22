CREATE PROCEDURE datadog.enable_events_statements_consumers()
SQL SECURITY DEFINER
BEGIN
  UPDATE performance_schema.setup_consumers SET enabled='YES' WHERE name LIKE 'events_statements_%';
  UPDATE performance_schema.setup_consumers SET enabled='YES' WHERE name='events_waits_current';
END
