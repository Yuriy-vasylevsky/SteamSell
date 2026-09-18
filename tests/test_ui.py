from types import SimpleNamespace

from app.i18n import tr
from app.ui import home_rows, persistent_menu, purchase_rows


def test_home_inline_menu_has_only_catalog_sections():
    rows = home_rows("ua")
    targets = [target for row in rows for _, target in row]
    assert "featured:0" in targets
    assert "catalog:0" in targets
    assert "purchases:0" not in targets
    assert "a:home" not in targets


def test_account_replaces_profile_actions_in_persistent_menu():
    markup = persistent_menu("ua", subscribed=True, loyalty_enabled=True)
    labels = [button.text for row in markup.keyboard for button in row]
    assert tr("account", "ua") in labels
    assert tr("language", "ua") not in labels
    assert tr("review", "ua") not in labels
    assert tr("loyalty", "ua") not in labels
    assert not any("розсил" in label.lower() for label in labels)


def test_admin_panel_stays_in_persistent_menu_for_admins():
    labels = [button.text for row in persistent_menu("ua", admin=True).keyboard for button in row]
    assert "⚙️ Адмін-панель" in labels


def test_code_button_is_hidden_without_gmail():
    order = SimpleNamespace(id="order-id")
    rows = purchase_rows(order, "ua", "support", gmail_connected=False)
    assert all(target != "code:order-id" for row in rows for _, target in row)
    rows = purchase_rows(order, "ua", "support", gmail_connected=True)
    assert any(target == "code:order-id" for row in rows for _, target in row)
    rows = purchase_rows(order, "ua", "support", gmail_connected=True, code_requests_remaining=0)
    assert any("ще 0" in label and target == "noop" for row in rows for label, target in row)


def test_alternative_activation_guide_comes_from_order_snapshot():
    order = SimpleNamespace(id="order-id", activation_type_snapshot="alternative")
    rows = purchase_rows(order, "ua", "support", gmail_connected=False)

    assert rows[0][0][1] == "alternative_activation_guide:order-id"


def test_zero_reissues_still_shows_one_primary_code_request():
    order = SimpleNamespace(id="order-id")
    rows = purchase_rows(
        order,
        "ua",
        "support",
        gmail_connected=True,
        code_requests_remaining=1,
        code_request_available=True,
    )
    assert any("ще 1" in label and target == "code:order-id" for row in rows for label, target in row)

    rows = purchase_rows(
        order,
        "ua",
        "support",
        gmail_connected=True,
        code_requests_remaining=0,
        code_request_available=False,
    )
    assert any("ще 0" in label and target == "noop" for row in rows for label, target in row)
