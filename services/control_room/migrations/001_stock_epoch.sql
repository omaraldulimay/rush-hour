CREATE SCHEMA IF NOT EXISTS control_room;

-- When the limited-drop stock was last reset. The oversold check only counts reservations made
-- after this moment, so a reset starts a clean sale without deleting any order history.
CREATE TABLE control_room.stock_epoch (
    id        integer PRIMARY KEY CHECK (id = 1),
    reset_at  timestamptz NOT NULL
);
