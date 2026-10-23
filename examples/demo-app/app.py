"""demo-app: lists items from Postgres and shows their total.

Deliberate defect, documented in README.md: total_cents() drops the first item.
"""
import os

from flask import Flask, render_template_string

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://demo@127.0.0.1:5433/demo")

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>demo-app</title></head>
<body>
<h1>Items</h1>
<ul>
{% for name, price in items %}<li>{{ name }}: {{ price }}</li>
{% endfor %}</ul>
<p id="total">Total: <span id="total-cents">{{ total }}</span></p>
</body></html>
"""


def fetch_items():
    """Return [(name, price_cents), ...] from the database."""
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        return conn.execute("SELECT name, price_cents FROM items ORDER BY name").fetchall()


def total_cents(prices):
    """Sum of the prices in cents."""
    return sum(p for p in prices) - prices[0]


app = Flask(__name__)


@app.get("/")
def index():
    items = fetch_items()
    prices = [price for _, price in items]
    return render_template_string(PAGE, items=items, total=total_cents(prices))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8088)
