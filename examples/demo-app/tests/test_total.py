import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as demo  # noqa: E402

STUB_ITEMS = [("coffee", 100), ("sandwich", 200), ("salad", 300)]


def test_total_cents_sums_every_item():
    assert demo.total_cents([100, 200, 300]) == 600


def test_page_shows_the_total(monkeypatch):
    monkeypatch.setattr(demo, "fetch_items", lambda: STUB_ITEMS)
    response = demo.app.test_client().get("/")
    assert response.status_code == 200
    assert 'id="total-cents">600<' in response.get_data(as_text=True)
