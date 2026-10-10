-- Driftwood Coffee Co. schema. Runs once, on first boot of an empty Postgres volume.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

CREATE TABLE products (
    id           serial PRIMARY KEY,
    sku          text NOT NULL UNIQUE,
    name         text NOT NULL,
    origin       text NOT NULL,
    roast        text NOT NULL,
    price_cents  integer NOT NULL CHECK (price_cents > 0),
    description  text NOT NULL DEFAULT '',
    stock        integer NOT NULL CHECK (stock >= 0)
);

CREATE TABLE orders (
    id              bigserial PRIMARY KEY,
    customer_email  text NOT NULL,            -- guest checkout: no customers table
    total_cents     integer NOT NULL,
    status          text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_orders_created_at ON orders (created_at);

CREATE TABLE order_items (
    order_id     bigint  NOT NULL REFERENCES orders (id),
    product_id   integer NOT NULL REFERENCES products (id),
    qty          integer NOT NULL CHECK (qty > 0),
    price_cents  integer NOT NULL
);
CREATE INDEX idx_order_items_order_id ON order_items (order_id);

-- Read-only role for the SRE agents: can read data and query statistics,
-- cannot write anything.
CREATE ROLE sre_readonly LOGIN PASSWORD 'sre_readonly';
GRANT pg_read_all_data, pg_read_all_stats TO sre_readonly;
