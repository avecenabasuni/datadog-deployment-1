<?php
declare(strict_types=1);
header('Content-Type: application/json');
header('Cache-Control: no-store');
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    header('Allow: GET');
    http_response_code(405);
    echo '{"error":"GET only"}';
    exit;
}
try {
    $pdo = new PDO(
        'mysql:host=' . getenv('DB_HOST') . ';dbname=' . getenv('DB_NAME') . ';charset=utf8mb4',
        getenv('DB_USER'),
        getenv('DB_PASSWORD'),
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
         PDO::ATTR_EMULATE_PREPARES => false,
         PDO::ATTR_TIMEOUT => 5]
    );
    $statement = $pdo->prepare('SELECT id, name, status FROM samples WHERE id >= ? ORDER BY id LIMIT 10');
    $statement->execute([1]);
    echo json_encode(['driver' => 'pdo_mysql', 'samples' => $statement->fetchAll(PDO::FETCH_ASSOC)], JSON_THROW_ON_ERROR);
} catch (Throwable $error) {
    http_response_code(503);
    // Do not print/log connection strings, credentials, or raw exception messages.
    error_log('Lab API database query failed');
    echo '{"error":"Database unavailable; inspect lab health and credentials"}';
}
