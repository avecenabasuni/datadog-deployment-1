USE eminerba_lab;
CREATE TABLE samples (
  id INT PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  status VARCHAR(20) NOT NULL
);
INSERT INTO samples (id, name, status) VALUES
  (1, 'Laboratory sample A', 'ready'),
  (2, 'Laboratory sample B', 'review'),
  (3, 'Laboratory sample C', 'ready');
REVOKE ALL PRIVILEGES ON eminerba_lab.* FROM 'eminerba_lab'@'%';
GRANT SELECT ON eminerba_lab.* TO 'eminerba_lab'@'%';
