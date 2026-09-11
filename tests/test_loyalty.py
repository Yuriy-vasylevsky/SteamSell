import pytest

from app.db import database
from app.models import Base, LoyaltyLevel, Order, User
from app.services import apply_discount, loyalty_status
from app.ui import discounted_price_text, priced_button_text


def test_discount_rounds_to_nearest_whole_hryvnia():
    assert apply_discount(100_00, 2) == 98_00
    assert apply_discount(99_99, 10) == 90_00
    assert apply_discount(105_00, 10) == 95_00
    assert apply_discount(104_00, 10) == 94_00
    assert apply_discount(100_00, 0) == 100_00


def test_discount_price_shows_regular_and_loyalty_price():
    text = discounted_price_text(100_00, 2)
    assert "100" in text
    assert "98" in text
    assert "-2%" in text


def test_product_button_keeps_price_when_title_is_long():
    label = priced_button_text("Гра " * 50, 100_00, 10)
    assert len(label) <= 64
    assert "90" in label
    assert "-10%" in label


@pytest.mark.asyncio
async def test_level_is_selected_from_successful_spending():
    engine, sessions = database("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions() as session, session.begin():
        session.add(User(id=1, first_name="Buyer"))
        session.add_all(
            [
                LoyaltyLevel(
                    level_number=1,
                    name_ua="Бронзовий",
                    name_ru="Бронзовый",
                    threshold_kopecks=100_000,
                    discount_percent=2,
                ),
                LoyaltyLevel(
                    level_number=2,
                    name_ua="Срібний",
                    name_ru="Серебряный",
                    threshold_kopecks=200_000,
                    discount_percent=4,
                ),
            ]
        )
        session.add(
            Order(
                id="a" * 32,
                user_id=1,
                product_id=1,
                product_name_snapshot="Game",
                price_snapshot=150_000,
                original_price_snapshot=150_000,
                status="delivered",
            )
        )
    async with sessions() as session:
        loyalty = await loyalty_status(session, 1)
        assert loyalty["current"].name_ua == "Бронзовий"
        assert loyalty["next"].name_ua == "Срібний"
        assert loyalty["discount_percent"] == 2
    await engine.dispose()
