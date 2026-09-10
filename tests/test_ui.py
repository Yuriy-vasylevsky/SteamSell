from types import SimpleNamespace

from app.ui import purchase_rows


def test_code_button_is_hidden_without_gmail():
    order = SimpleNamespace(id="order-id")
    rows = purchase_rows(order, "ua", "support", gmail_connected=False)
    assert all(target != "code:order-id" for row in rows for _, target in row)
    rows = purchase_rows(order, "ua", "support", gmail_connected=True)
    assert any(target == "code:order-id" for row in rows for _, target in row)
    rows = purchase_rows(order, "ua", "support", gmail_connected=True, code_requests_remaining=0)
    assert any("ще 0" in label and target == "noop" for row in rows for label, target in row)
