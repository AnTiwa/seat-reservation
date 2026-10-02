CREATE EXTENSION IF NOT EXISTS pgcrypto;


-- ============================================================
-- SHOWS
-- ============================================================

CREATE TABLE IF NOT EXISTS shows (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    name TEXT NOT NULL,

    price_paise BIGINT NOT NULL
        CHECK (price_paise >= 0),

    per_user_limit INTEGER NOT NULL DEFAULT 4
        CHECK (per_user_limit > 0),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ============================================================
-- RESERVATIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    show_id UUID NOT NULL
        REFERENCES shows(id),

    user_id TEXT NOT NULL,

    amount_paise BIGINT NOT NULL
        CHECK (amount_paise >= 0),

    status TEXT NOT NULL
        CHECK (status IN ('confirmed', 'cancelled')),

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    cancelled_at TIMESTAMPTZ NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_show_user_status
    ON reservations(show_id, user_id, status);


-- ============================================================
-- SEATS
-- ============================================================

CREATE TABLE IF NOT EXISTS seats (
    show_id UUID NOT NULL
        REFERENCES shows(id)
        ON DELETE CASCADE,

    seat_number TEXT NOT NULL,

    status TEXT NOT NULL DEFAULT 'available'
        CHECK (status IN ('available', 'held', 'confirmed')),

    reservation_id UUID NULL
        REFERENCES reservations(id)
        ON DELETE SET NULL,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (show_id, seat_number)
);

CREATE INDEX IF NOT EXISTS idx_seats_show_status
    ON seats(show_id, status);


-- ============================================================
-- RESERVATION SEATS
-- ============================================================

CREATE TABLE IF NOT EXISTS reservation_seats (
    reservation_id UUID NOT NULL
        REFERENCES reservations(id)
        ON DELETE CASCADE,

    show_id UUID NOT NULL,

    seat_number TEXT NOT NULL,

    PRIMARY KEY (reservation_id, seat_number),

    CONSTRAINT fk_reservation_seat
        FOREIGN KEY (show_id, seat_number)
        REFERENCES seats(show_id, seat_number)
        ON DELETE RESTRICT
);


-- ============================================================
-- IDEMPOTENCY
-- ============================================================

CREATE TABLE IF NOT EXISTS idempotency_keys (
    show_id UUID NOT NULL
        REFERENCES shows(id)
        ON DELETE CASCADE,

    idempotency_key TEXT NOT NULL,

    user_id TEXT NOT NULL,

    request_hash TEXT NOT NULL,

    reservation_id UUID NULL
        REFERENCES reservations(id)
        ON DELETE SET NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (show_id, idempotency_key)
);


-- ============================================================
-- USER / SHOW COUNTER
-- ============================================================

CREATE TABLE IF NOT EXISTS user_show_counters (
    show_id UUID NOT NULL
        REFERENCES shows(id)
        ON DELETE CASCADE,

    user_id TEXT NOT NULL,

    seat_count INTEGER NOT NULL DEFAULT 0
        CHECK (seat_count >= 0),

    PRIMARY KEY (show_id, user_id)
);
