<?php

declare(strict_types=1);

const MAX_BODY_BYTES = 65536;
const MAX_QUERY_LIMIT = 25000;

$webhookToken = 'replace-with-a-random-secret';

header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');

set_exception_handler(static function (Throwable $error): void {
    error_log((string) $error);
    respond(500, ['error' => 'internal_server_error']);
});

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$path = parse_url($_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH) ?: '/';
$action = isset($_GET['action']) && is_string($_GET['action'])
    ? $_GET['action']
    : '';

if ($method === 'OPTIONS') {
    header('Allow: GET, POST, OPTIONS');
    respond(204, null);
}

if ($method === 'GET'
    && ($action === 'dashboard'
        || ($action === '' && ($path === '/' || str_ends_with($path, '/index.php'))))) {
    serveDashboard();
}

try {
    $pdo = connectDatabase();

    if ($method === 'POST' && ($action === 'webhook' || $path === '/webhook')) {
        authorizeWebhook($webhookToken);
        createReading($pdo, readJsonBody());
    }

    if ($method === 'GET'
        && ($action === 'readings' || $path === '/api/readings')) {
        listReadings($pdo);
    }

    if ($method === 'GET'
        && ($action === 'latest' || $path === '/api/readings/latest')) {
        latestReading($pdo);
    }

    if ($method === 'GET' && ($action === 'health' || $path === '/health')) {
        $pdo->query('SELECT 1');
        respond(200, ['status' => 'ok']);
    }

    respond(404, ['error' => 'not_found']);
} catch (InvalidArgumentException $error) {
    respond(422, ['error' => 'validation_error', 'message' => $error->getMessage()]);
} catch (JsonException $error) {
    respond(400, ['error' => 'invalid_json', 'message' => $error->getMessage()]);
} catch (PDOException $error) {
    error_log((string) $error);
    respond(503, ['error' => 'database_unavailable']);
}

function connectDatabase(): PDO
{
    $host = envValue('DB_HOST', 'localhost');
    $port = envValue('DB_PORT', '3306');
    $name = envValue('DB_NAME', 'zocs_playground');
    $user = envValue('DB_USER', 'zocs_playground');
    $password = envValue('DB_PASSWORD', 'zocs_playground');

    $dsn = sprintf(
        'mysql:host=%s;port=%s;dbname=%s;charset=utf8mb4',
        $host,
        $port,
        $name,
    );

    return new PDO($dsn, $user, $password, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES => false,
        PDO::ATTR_STRINGIFY_FETCHES => false,
    ]);
}

function authorizeWebhook(string $expected): void
{
    if ($expected === '') {
        return;
    }

    $authorization = $_SERVER['HTTP_AUTHORIZATION'] ?? '';
    if (!preg_match('/^Bearer\s+(.+)$/i', $authorization, $matches)
        || !hash_equals($expected, $matches[1])) {
        respond(401, ['error' => 'unauthorized']);
    }
}

/**
 * @return array<string, mixed>
 */
function readJsonBody(): array
{
    $contentLength = (int) ($_SERVER['CONTENT_LENGTH'] ?? 0);
    if ($contentLength > MAX_BODY_BYTES) {
        respond(413, ['error' => 'payload_too_large']);
    }

    $raw = file_get_contents('php://input', false, null, 0, MAX_BODY_BYTES + 1);
    if ($raw === false || $raw === '') {
        throw new InvalidArgumentException('request body is required');
    }
    if (strlen($raw) > MAX_BODY_BYTES) {
        respond(413, ['error' => 'payload_too_large']);
    }

    $data = json_decode($raw, true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($data) || isListArray($data)) {
        throw new InvalidArgumentException('JSON body must be an object');
    }

    return $data;
}

/**
 * PHP 8.0-compatible equivalent of array_is_list().
 *
 * @param array<mixed> $value
 */
function isListArray(array $value): bool
{
    $expectedKey = 0;
    foreach ($value as $key => $_) {
        if ($key !== $expectedKey) {
            return false;
        }
        $expectedKey++;
    }

    return true;
}

/**
 * @param array<string, mixed> $data
 */
function createReading(PDO $pdo, array $data): never
{
    $timestamp = parseTimestamp(requiredString($data, 'timestamp'), 'timestamp');
    $address = normalizeMac(requiredString($data, 'address'));
    $model = requiredString($data, 'model', 32);
    $productId = requiredString($data, 'product_id', 10);
    if (!preg_match('/^0x[0-9A-Fa-f]{4}$/', $productId)) {
        throw new InvalidArgumentException('product_id must look like 0x55B5');
    }

    $temperature = nullableNumber($data, 'temperature', -100, 150);
    $humidity = nullableNumber($data, 'humidity', 0, 100);
    $battery = nullableNumber($data, 'battery', 0, 100);
    $rssi = nullableNumber($data, 'rssi', -200, 0);
    if ($temperature === null && $humidity === null) {
        throw new InvalidArgumentException(
            'at least one of temperature or humidity is required',
        );
    }

    $statement = $pdo->prepare(
        'INSERT INTO mijia_readings
            (measured_at, address, model, product_id, temperature, humidity, battery, rssi)
         VALUES
            (:measured_at, :address, :model, :product_id, :temperature, :humidity, :battery, :rssi)',
    );
    $statement->execute([
        'measured_at' => $timestamp,
        'address' => $address,
        'model' => $model,
        'product_id' => strtoupper($productId),
        'temperature' => $temperature,
        'humidity' => $humidity,
        'battery' => $battery,
        'rssi' => $rssi,
    ]);

    respond(201, [
        'ok' => true,
        'id' => (int) $pdo->lastInsertId(),
    ]);
}

function listReadings(PDO $pdo): never
{
    $limit = queryInteger('limit', 100, 1, MAX_QUERY_LIMIT);
    $conditions = [];
    $parameters = [];

    if (isset($_GET['address']) && $_GET['address'] !== '') {
        if (!is_string($_GET['address'])) {
            throw new InvalidArgumentException('address must be a string');
        }
        $conditions[] = 'address = :address';
        $parameters['address'] = normalizeMac($_GET['address']);
    }

    if (isset($_GET['from']) && $_GET['from'] !== '') {
        if (!is_string($_GET['from'])) {
            throw new InvalidArgumentException('from must be a timestamp');
        }
        $conditions[] = 'measured_at >= :from_time';
        $parameters['from_time'] = parseTimestamp($_GET['from'], 'from');
    }

    if (isset($_GET['to']) && $_GET['to'] !== '') {
        if (!is_string($_GET['to'])) {
            throw new InvalidArgumentException('to must be a timestamp');
        }
        $conditions[] = 'measured_at <= :to_time';
        $parameters['to_time'] = parseTimestamp($_GET['to'], 'to');
    }

    $where = $conditions === [] ? '' : ' WHERE ' . implode(' AND ', $conditions);
    $sql = 'SELECT id, measured_at, address, model, product_id,
                   temperature, humidity, battery, rssi, received_at
            FROM mijia_readings'
        . $where
        . ' ORDER BY id DESC LIMIT '
        . $limit;

    $statement = $pdo->prepare($sql);
    $statement->execute($parameters);
    $items = array_map('serializeReading', $statement->fetchAll());

    respond(200, [
        'items' => $items,
        'count' => count($items),
    ]);
}

function latestReading(PDO $pdo): never
{
    $parameters = [];
    $conditions = [];

    if (isset($_GET['address']) && $_GET['address'] !== '') {
        if (!is_string($_GET['address'])) {
            throw new InvalidArgumentException('address must be a string');
        }
        $conditions[] = 'address = :address';
        $parameters['address'] = normalizeMac($_GET['address']);
    }

    if (isset($_GET['field']) && $_GET['field'] !== '') {
        if (!is_string($_GET['field'])
            || !in_array($_GET['field'], ['temperature', 'humidity', 'battery'], true)) {
            throw new InvalidArgumentException(
                'field must be temperature, humidity, or battery',
            );
        }
        $conditions[] = $_GET['field'] . ' IS NOT NULL';
    }

    $where = $conditions === [] ? '' : ' WHERE ' . implode(' AND ', $conditions);
    $statement = $pdo->prepare(
        'SELECT id, measured_at, address, model, product_id,
                temperature, humidity, battery, rssi, received_at
         FROM mijia_readings'
        . $where
        . ' ORDER BY id DESC LIMIT 1',
    );
    $statement->execute($parameters);
    $reading = $statement->fetch();
    if ($reading === false) {
        respond(404, ['error' => 'reading_not_found']);
    }

    respond(200, serializeReading($reading));
}

function serveDashboard(): never
{
    $path = __DIR__ . DIRECTORY_SEPARATOR . 'dashboard.html';
    $html = file_get_contents($path);
    if ($html === false) {
        respond(500, ['error' => 'dashboard_unavailable']);
    }

    header('Content-Type: text/html; charset=utf-8', true);
    header('Cache-Control: no-cache');
    echo $html;
    exit;
}

/**
 * @param array<string, mixed> $data
 */
function requiredString(array $data, string $key, int $maxLength = 64): string
{
    $value = $data[$key] ?? null;
    if (!is_string($value) || trim($value) === '') {
        throw new InvalidArgumentException($key . ' must be a non-empty string');
    }
    $value = trim($value);
    if (strlen($value) > $maxLength) {
        throw new InvalidArgumentException($key . ' is too long');
    }

    return $value;
}

/**
 * @param array<string, mixed> $data
 */
function nullableNumber(
    array $data,
    string $key,
    float $minimum,
    float $maximum,
): ?float {
    $value = $data[$key] ?? null;
    if ($value === null) {
        return null;
    }
    if (!is_int($value) && !is_float($value)) {
        throw new InvalidArgumentException($key . ' must be a number or null');
    }
    $value = (float) $value;
    if (!is_finite($value) || $value < $minimum || $value > $maximum) {
        throw new InvalidArgumentException(
            sprintf('%s must be between %g and %g', $key, $minimum, $maximum),
        );
    }

    return $value;
}

function normalizeMac(string $value): string
{
    $normalized = strtoupper(str_replace('-', ':', trim($value)));
    if (!preg_match('/^[0-9A-F]{2}(?::[0-9A-F]{2}){5}$/', $normalized)) {
        throw new InvalidArgumentException('address must be a Bluetooth MAC address');
    }

    return $normalized;
}

function parseTimestamp(string $value, string $field): string
{
    try {
        $timestamp = new DateTimeImmutable($value);
    } catch (Exception) {
        throw new InvalidArgumentException($field . ' must be an ISO 8601 timestamp');
    }

    return $timestamp
        ->setTimezone(new DateTimeZone('UTC'))
        ->format('Y-m-d H:i:s.u');
}

function queryInteger(string $key, int $default, int $minimum, int $maximum): int
{
    if (!isset($_GET[$key]) || $_GET[$key] === '') {
        return $default;
    }
    if (!is_string($_GET[$key])
        || !preg_match('/^[0-9]+$/', $_GET[$key])) {
        throw new InvalidArgumentException($key . ' must be an integer');
    }
    $value = (int) $_GET[$key];
    if ($value < $minimum || $value > $maximum) {
        throw new InvalidArgumentException(
            sprintf('%s must be between %d and %d', $key, $minimum, $maximum),
        );
    }

    return $value;
}

/**
 * @param array<string, mixed> $row
 * @return array<string, mixed>
 */
function serializeReading(array $row): array
{
    return [
        'id' => (int) $row['id'],
        'timestamp' => databaseTimeToIso((string) $row['measured_at']),
        'address' => $row['address'],
        'model' => $row['model'],
        'product_id' => $row['product_id'],
        'temperature' => nullableFloat($row['temperature']),
        'humidity' => nullableFloat($row['humidity']),
        'battery' => nullableFloat($row['battery']),
        'rssi' => nullableFloat($row['rssi']),
        'received_at' => databaseTimeToIso((string) $row['received_at']),
    ];
}

function nullableFloat(mixed $value): ?float
{
    return $value === null ? null : (float) $value;
}

function databaseTimeToIso(string $value): string
{
    return (new DateTimeImmutable($value, new DateTimeZone('UTC')))
        ->format('Y-m-d\TH:i:s.u\Z');
}

function envValue(string $name, string $default): string
{
    $value = getenv($name);
    return $value === false || $value === '' ? $default : $value;
}

/**
 * @param array<string, mixed>|null $body
 */
function respond(int $status, ?array $body): never
{
    http_response_code($status);
    if ($body !== null) {
        echo json_encode(
            $body,
            JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR,
        );
    }
    exit;
}
