from datetime import UTC, datetime, timedelta
from io import BytesIO
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from PIL import Image

from app.receipts import ReceiptError, _json_object, analyze_receipt, evaluate_receipt, prepare_receipt


def png(width=400, height=600):
    output = BytesIO()
    Image.new("RGB", (width, height), "#263238").save(output, format="PNG")
    return output.getvalue()


def analysis(**changes):
    value = {
        "is_payment_receipt": True,
        "status": "success",
        "amount_uah": 5,
        "recipient_card_last4": "5077",
        "payment_datetime": datetime.now(UTC).isoformat(),
        "confidence": 0.97,
        "reason": "видно успішну оплату",
        "recipient_iban": "UA000000000000000000000000000",
    }
    return value | changes


def test_receipt_requires_amount_card_status_time_and_confidence():
    created = datetime.now(UTC) - timedelta(minutes=2)
    assert evaluate_receipt(analysis(), 500, {"5077", "1234"}, created)[0]
    for changes in (
        {"amount_uah": 4.99},
        {"recipient_card_last4": "9999"},
        {"payment_datetime": (created - timedelta(hours=1)).isoformat()},
        {"confidence": 0.89},
    ):
        assert not evaluate_receipt(analysis(**changes), 500, {"5077"}, created)[0]


def test_transaction_status_does_not_affect_receipt_result():
    created = datetime.now(UTC) - timedelta(minutes=2)
    for status in (None, "unknown", "pending", "failed"):
        approved, reason = evaluate_receipt(analysis(status=status), 500, {"5077"}, created)
        assert approved, reason


def test_rejection_reason_contains_detected_mismatch():
    created = datetime.now(UTC) - timedelta(minutes=2)
    _, amount_reason = evaluate_receipt(analysis(amount_uah=4.99), 500, {"5077"}, created)
    _, card_reason = evaluate_receipt(
        analysis(recipient_card_last4="9999"), 500, {"5077"}, created
    )
    assert "4.99" in amount_reason and "5.00" in amount_reason
    assert "9999" in card_reason


def test_missing_time_is_rejected():
    created = datetime.now(UTC) - timedelta(minutes=2)
    approved, reason = evaluate_receipt(
        analysis(payment_datetime=None), 500, {"5077"}, created
    )
    assert not approved
    assert "Час" in reason


def test_receipt_classification_is_ignored():
    created = datetime.now(UTC) - timedelta(minutes=2)
    approved, reason = evaluate_receipt(
        analysis(is_payment_receipt=False), 500, {"5077"}, created
    )
    assert approved, reason


def test_negative_amount_and_commission_are_supported():
    created = datetime.now(UTC) - timedelta(minutes=2)
    negative, negative_reason = evaluate_receipt(
        analysis(amount_uah=-5), 500, {"5077"}, created
    )
    with_fee, fee_reason = evaluate_receipt(
        analysis(amount_uah=-5.50, fee_uah=0.50), 500, {"5077"}, created
    )
    with_total, total_reason = evaluate_receipt(
        analysis(amount_uah=-5, fee_uah=0.50, total_debited_uah=-5.50),
        500,
        {"5077"},
        created,
    )
    assert negative, negative_reason
    assert with_fee, fee_reason
    assert with_total, total_reason


def test_visible_payment_time_must_be_within_ten_minutes():
    created = datetime.now(UTC)
    recent = analysis(payment_datetime=(datetime.now(UTC) - timedelta(minutes=9)).isoformat())
    old = analysis(payment_datetime=(datetime.now(UTC) - timedelta(minutes=11)).isoformat())
    assert evaluate_receipt(recent, 500, {"5077"}, created)[0]
    approved, reason = evaluate_receipt(old, 500, {"5077"}, created)
    assert not approved
    assert "10 хв" in reason


def test_unreadable_field_has_a_specific_reason():
    created = datetime.now(UTC) - timedelta(minutes=2)
    _, reason = evaluate_receipt(analysis(amount_uah=None), 500, {"5077"}, created)
    assert "Суму" in reason
    assert "картку або час" not in reason


def test_iban_is_ignored_and_image_hash_is_stable():
    created = datetime.now(UTC) - timedelta(minutes=1)
    assert evaluate_receipt(analysis(recipient_iban="WRONG"), 500, {"5077"}, created)[0]
    original = png()
    image = prepare_receipt(original)
    assert image.sha256 == prepare_receipt(original).sha256
    with Image.open(BytesIO(image.data)) as enhanced:
        assert enhanced.width == 1200
        assert enhanced.height == 1800
        assert image.mime == "image/png"


def test_matching_iban_can_replace_recipient_card_check():
    created = datetime.now(UTC) - timedelta(minutes=1)
    iban = "UA123456789012345678901234567"
    approved, reason = evaluate_receipt(
        analysis(recipient_card_last4=None, recipient_iban=iban),
        500,
        {"5077"},
        created,
        {iban},
    )
    assert approved, reason


def test_unknown_iban_is_rejected_without_matching_card():
    created = datetime.now(UTC) - timedelta(minutes=1)
    approved, reason = evaluate_receipt(
        analysis(recipient_card_last4=None, recipient_iban="UA000000000000000000000000000"),
        500,
        {"5077"},
        created,
        {"UA123456789012345678901234567"},
    )
    assert not approved
    assert "IBAN" in reason


def test_json_object_is_extracted_from_markdown_or_extra_text():
    assert _json_object('Result:\n```json\n{"status":"success"}\n```')["status"] == "success"


@pytest.mark.asyncio
async def test_analysis_retries_when_amount_is_null():
    incomplete = analysis(amount_uah=None)
    complete = analysis(amount_uah=2.0)
    responses = []
    for result in (incomplete, complete):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "choices": [{"message": {"content": __import__("json").dumps(result)}}]
        }
        responses.append(response)
    client = AsyncMock()
    client.post.side_effect = responses
    result = await analyze_receipt(
        client, "test-key", "test-model", prepare_receipt(png()), timeout=45
    )
    assert result["amount_uah"] == 2.0
    assert client.post.await_count == 2
    assert client.post.await_args_list[0].kwargs["timeout"] == 45
    retry_prompt = client.post.await_args_list[1].kwargs["json"]["messages"][0]["content"][0]["text"]
    assert "Сума платежу" in retry_prompt


@pytest.mark.asyncio
async def test_analysis_reports_specific_invalid_response_reason():
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"choices": [{"message": {"content": "plain text"}}]}
    client = AsyncMock()
    client.post.return_value = response
    with pytest.raises(ReceiptError, match="немає JSON-об'єкта"):
        await analyze_receipt(
            client, "test-key", "test-model", prepare_receipt(png())
        )
    assert client.post.await_count == 3


@pytest.mark.asyncio
async def test_authentication_failure_is_not_retried():
    response = httpx.Response(401, request=httpx.Request("POST", "https://example.com"))
    client = AsyncMock()
    client.post.return_value = response
    with pytest.raises(ReceiptError, match="HTTP 401"):
        await analyze_receipt(client, "test-key", "test-model", prepare_receipt(png()))
    assert client.post.await_count == 1


@pytest.mark.asyncio
async def test_readable_but_incomplete_receipt_stops_after_second_attempt():
    response = Mock()
    response.json.return_value = {
        "choices": [{"message": {"content": __import__("json").dumps(analysis(amount_uah=None))}}]
    }
    client = AsyncMock()
    client.post.return_value = response
    result = await analyze_receipt(client, "test-key", "test-model", prepare_receipt(png()))
    assert result["amount_uah"] is None
    assert client.post.await_count == 2
