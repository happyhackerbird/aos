INSERT INTO items (name, price_cents) VALUES
    ('coffee', 100),
    ('sandwich', 200),
    ('salad', 300)
ON CONFLICT (name) DO UPDATE SET price_cents = EXCLUDED.price_cents;
