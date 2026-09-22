CREATE PROCEDURE {schema}.explain_statement(IN query TEXT)
SQL SECURITY DEFINER
BEGIN
  SET @explain := CONCAT('EXPLAIN FORMAT=json ', query);
  PREPARE stmt FROM @explain;
  EXECUTE stmt;
  DEALLOCATE PREPARE stmt;
END
