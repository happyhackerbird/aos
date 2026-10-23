"""The release checker must distinguish this bus's compute from other runs."""
import hashlib
import subprocess
from types import SimpleNamespace

import pytest

from scripts import release_matrix


@pytest.mark.parametrize("override", [None, "r7x"])
def test_leftover_check_uses_bus_prefix_and_preserves_other_buses(monkeypatch, override):
    url = "http://127.0.0.1:8047"
    prefix = override or "a" + hashlib.sha256(b"127.0.0.1:8047").hexdigest()[:2]
    env = {} if override is None else {"AOS_OPENSHELL_SANDBOX_PREFIX": override}
    driver = object.__new__(release_matrix.Driver)
    driver.args = SimpleNamespace(env=env)
    driver.bus = SimpleNamespace(url=url)
    owned = [f"{prefix}-ao-1-r1", f"{prefix}-ao-1-r1-acc"]
    listing = "NAME STATUS\n" + "\n".join(
        f"{name} running" for name in [*owned, "other-ao-1-r1", f"{prefix}-ao-2-r1"])
    monkeypatch.setattr(release_matrix, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 0, listing, ""))
    assert driver.sandboxes_left({"runs": [{"id": "AO-1-r1"}]}) == sorted(owned)


def test_failed_sandbox_inventory_is_not_clean(monkeypatch):
    driver = object.__new__(release_matrix.Driver)
    monkeypatch.setattr(release_matrix, "run", lambda *a, **kw:
                        subprocess.CompletedProcess(a, 1, "", "unavailable"))
    assert driver.sandboxes_left({"runs": [{"id": "AO-1-r1"}]}) is None
