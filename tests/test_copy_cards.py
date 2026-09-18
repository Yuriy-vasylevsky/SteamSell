from types import SimpleNamespace

from app.ui import copy_card_rows, keyboard, payment_rows, payment_wait_text


def test_payment_card_copy_button_copies_only_card_number():
    order = SimpleNamespace(id="order-id", payment_method="personal")
    rows = payment_rows(order, "ua", ["Mono: 4444 1111 2222 3333"])
    copy_button = keyboard(rows).inline_keyboard[0][0]

    assert copy_button.copy_text.text == "4444111122223333"
    assert "3333" in copy_button.text


def test_copy_card_rows_skip_duplicate_numbers():
    rows = copy_card_rows(["4444 1111 2222 3333", "4444111122223333"], "ua")
    assert len(rows) == 1


def test_receipt_payment_keeps_card_number_in_its_own_code_element():
    order = SimpleNamespace(
        payment_method="receipt",
        product_name_snapshot="Product",
        original_price_snapshot=10000,
        price_snapshot=10000,
        discount_percent_snapshot=0,
        promo_code_snapshot=None,
        promo_discount_percent_snapshot=0,
        tip_percent_snapshot=0,
    )
    text = payment_wait_text(order, "ua", card="<b>Mono:</b> <code>4444111122223333</code>")

    assert "<b>Mono:</b> <code>4444111122223333</code>" in text
    assert "&lt;code&gt;" not in text
