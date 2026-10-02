from prometheus_client import Counter, Gauge, Histogram


reservations_confirmed = Counter(
    "reservations_confirmed_total",
    "Total number of reservations successfully confirmed",
)

reservations_declined = Counter(
    "reservations_declined_total",
    "Total number of reservation attempts declined",
    ["reason"],  # seat-taken | per-user-limit | idempotent-replay | seat-not-found | invalid-request
)

request_errors = Counter(
    "http_5xx_total",
    "Total number of HTTP 5xx responses returned",
)

seats_available = Gauge(
    "seats_available",
    "Number of seats currently in 'available' state",
    ["show_id"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "End-to-end HTTP request duration in seconds",
    ["method", "path_template", "status_class"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
