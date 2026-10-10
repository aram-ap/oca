#!/bin/bash
# Seed the catalogue and ~6 months of order history.
# SEED_ORDERS and SEED_CUSTOMERS come from docker-compose (see .env).
set -euo pipefail

ORDERS="${SEED_ORDERS:-2000000}"
CUSTOMERS="${SEED_CUSTOMERS:-250000}"
echo "seeding ${ORDERS} orders across ${CUSTOMERS} customers..."

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v orders="$ORDERS" -v customers="$CUSTOMERS" <<'SQL'
INSERT INTO products (sku, name, origin, roast, price_cents, description, stock) VALUES
 ('DW-ETH-YIR', 'Yirgacheffe Kochere',   'Ethiopia',   'light',  2100, 'Jasmine, bergamot, lemon curd.',        1000000000),
 ('DW-ETH-GUJ', 'Guji Hambela',          'Ethiopia',   'light',  2200, 'Peach, black tea, honey.',              1000000000),
 ('DW-KEN-NYE', 'Nyeri Gichathaini AA',  'Kenya',      'light',  2400, 'Blackcurrant, grapefruit, cane sugar.', 1000000000),
 ('DW-COL-HUI', 'Huila Finca El Paraiso','Colombia',   'medium', 1900, 'Red apple, caramel, cocoa.',            1000000000),
 ('DW-COL-NAR', 'Nariño La Esperanza',   'Colombia',   'medium', 1850, 'Cherry, panela, almond.',               1000000000),
 ('DW-GUA-ANT', 'Antigua San Miguel',    'Guatemala',  'medium', 1800, 'Milk chocolate, orange, toffee.',       1000000000),
 ('DW-CRI-TAR', 'Tarrazú Las Lajas',     'Costa Rica', 'medium', 2000, 'Honey, apricot, brown sugar.',          1000000000),
 ('DW-BRA-CER', 'Cerrado Mineiro',       'Brazil',     'dark',   1500, 'Hazelnut, dark chocolate, low acidity.',1000000000),
 ('DW-SUM-GAY', 'Sumatra Gayo',          'Indonesia',  'dark',   1700, 'Cedar, molasses, earthy.',              1000000000),
 ('DW-PER-CAJ', 'Cajamarca Cooperative', 'Peru',       'medium', 1750, 'Plum, walnut, cocoa nib.',              1000000000),
 ('DW-RWA-KIV', 'Lake Kivu Nyamasheke',  'Rwanda',     'light',  2150, 'Red grape, hibiscus, cane sugar.',      1000000000),
 ('DW-HOU-ESP', 'House Espresso',        'Blend',      'medium', 1650, 'Chocolate, cherry, syrupy body.',       1000000000),
 ('DW-HOU-DRK', 'Midnight Blend',        'Blend',      'dark',   1600, 'Smoky, bittersweet, heavy body.',       1000000000),
 ('DW-DEC-SWP', 'Swiss Water Decaf',     'Colombia',   'medium', 1800, 'Caramel, red apple, mild.',             1000000000);

-- Orders spread over the last 180 days, skewed toward recent days.
INSERT INTO orders (customer_email, total_cents, status, created_at)
SELECT 'customer' || (1 + floor(random() * :customers))::int || '@example.com',
       0,
       'paid',
       now() - (power(random(), 1.3) * interval '180 days')
FROM generate_series(1, :orders);

-- 1 to 3 line items per order, popular products more likely.
INSERT INTO order_items (order_id, product_id, qty, price_cents)
SELECT o.id, p.id, li.qty, p.price_cents
FROM orders o
CROSS JOIN LATERAL (
    SELECT 1 + floor(power(random(), 1.6) * 14)::int AS product_id,
           1 + floor(random() * 3)::int            AS qty
    FROM generate_series(1, 1 + floor(random() * 3)::int + (o.id * 0)::int)
) li
JOIN products p ON p.id = li.product_id;

UPDATE orders o SET total_cents = t.total
FROM (SELECT order_id, sum(qty * price_cents) AS total FROM order_items GROUP BY order_id) t
WHERE t.order_id = o.id;

VACUUM ANALYZE;
SQL
echo "seed complete"
