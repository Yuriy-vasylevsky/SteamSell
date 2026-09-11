import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(256), default="")
    language: Mapped[str | None] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    access_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    broadcast_subscribed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("price > 0"),
        CheckConstraint("code_limit BETWEEN 0 AND 5"),
        CheckConstraint("stock_quantity IS NULL OR stock_quantity >= 0"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name_ua: Mapped[str] = mapped_column(String(150))
    name_ru: Mapped[str] = mapped_column(String(150))
    description_ua: Mapped[str] = mapped_column(Text, default="")
    description_ru: Mapped[str] = mapped_column(Text, default="")
    price: Mapped[int] = mapped_column(Integer)  # UAH kopecks, never floating point
    code_limit: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    stock_quantity: Mapped[int | None] = mapped_column(Integer)
    image_file_id: Mapped[str | None] = mapped_column(Text)
    delivery_mode: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    steam_login_encrypted: Mapped[str | None] = mapped_column(Text)
    steam_password_encrypted: Mapped[str | None] = mapped_column(Text)
    gmail_credentials_encrypted: Mapped[str | None] = mapped_column(Text)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    on_home: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    featured_position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("price_snapshot > 0"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(150))
    price_snapshot: Mapped[int] = mapped_column(Integer)
    original_price_snapshot: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    discount_percent_snapshot: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tip_percent_snapshot: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    delivery_mode_snapshot: Mapped[str] = mapped_column(String(16), default="auto", server_default="auto")
    status: Mapped[str] = mapped_column(String(24), default="waiting_payment", index=True)
    mono_invoice_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    payment_message_id: Mapped[int | None] = mapped_column(BigInteger)
    payment_animation_step: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    payment_animation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_method: Mapped[str] = mapped_column(String(16), default="acquiring", server_default="acquiring")
    payment_cards_encrypted: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_message_id: Mapped[int | None] = mapped_column(BigInteger)
    delivery_uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_notified: Mapped[bool] = mapped_column(Boolean, default=False)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class LoyaltyLevel(Base):
    __tablename__ = "loyalty_levels"
    __table_args__ = (
        CheckConstraint("level_number BETWEEN 1 AND 5"),
        CheckConstraint("threshold_kopecks >= 0"),
        CheckConstraint("discount_percent BETWEEN 0 AND 50"),
    )
    level_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    name_ua: Mapped[str] = mapped_column(String(64))
    name_ru: Mapped[str] = mapped_column(String(64))
    threshold_kopecks: Mapped[int] = mapped_column(Integer)
    discount_percent: Mapped[int] = mapped_column(Integer)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), unique=True)
    invoice_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PaymentCard(Base):
    __tablename__ = "payment_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(64))
    number_encrypted: Mapped[str] = mapped_column(Text)
    last4: Mapped[str] = mapped_column(String(4))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PaymentReceipt(Base):
    __tablename__ = "payment_receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="analyzing", index=True)
    analysis: Mapped[dict | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(String(256))
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminLog(Base):
    __tablename__ = "admin_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MailCodeRequest(Base):
    __tablename__ = "mail_code_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.id"), index=True)
    outcome: Mapped[str] = mapped_column(String(32))
    message_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Broadcast(Base):
    __tablename__ = "broadcasts"
    id: Mapped[int] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued")
    last_user_id: Mapped[int] = mapped_column(BigInteger, default=0)
    delivered: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
