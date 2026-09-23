<?php
declare(strict_types=1);
header('Content-Type: application/json');
header('Cache-Control: no-store');
// Lab-only, allowlisted HTTP SAPI diagnostics. Never expose phpinfo or passwords.
$socket = '/var/run/datadog/apm.socket';
$connection = @stream_socket_client('unix://' . $socket, $error, $message, 1);
if ($connection) {
    fclose($connection);
}
echo json_encode([
    'php' => PHP_VERSION,
    'sapi' => PHP_SAPI,
    'tracer' => phpversion('ddtrace') ?: null,
    'pdo_drivers' => PDO::getAvailableDrivers(),
    'env' => getenv('DD_ENV') ?: null,
    'service' => getenv('DD_SERVICE') ?: null,
    'version' => getenv('DD_VERSION') ?: null,
    'dbm_propagation' => getenv('DD_DBM_PROPAGATION_MODE') ?: null,
    'apm_socket_connected' => (bool) $connection,
], JSON_THROW_ON_ERROR);
