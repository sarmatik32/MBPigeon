from __future__ import annotations

import enum
import uuid
from datetime import datetime, date
from typing import Optional

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, Date, DateTime, Enum, ForeignKey,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator, CHAR


class GUID(TypeDecorator):
    """Portable UUID: native on Postgres, CHAR(36) on SQLite."""
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _jsonb():
    """JSONB on Postgres, JSON elsewhere."""
    return JSON().with_variant(JSONB(), "postgresql")


def _bigpk():
    """BIGINT PK on Postgres, INTEGER PK on SQLite (needed for autoincrement)."""
    return BigInteger().with_variant(Integer(), "sqlite")


class Language(str, enum.Enum):
    uk = "uk"; en = "en"; es = "es"; fr = "fr"; de = "de"; zh = "zh"; ru = "ru"


class PerkCategory(str, enum.Enum):
    cosmetic = "cosmetic"
    consumable = "consumable"
    access = "access"
    subscription = "subscription"


class TxnType(str, enum.Enum):
    earn = "earn"
    spend = "spend"
    refund = "refund"
    admin_adjust = "admin_adjust"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True)
    language: Mapped[Language] = mapped_column(Enum(Language, name="language_code"), default=Language.uk, nullable=False)
    coin_balance: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_daily_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    elo: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)

    __table_args__ = (CheckConstraint("coin_balance >= 0", name="ck_users_balance_nonneg"),)

    perks: Mapped[list["UserPerk"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Perk(Base):
    __tablename__ = "perks"

    perk_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    category: Mapped[PerkCategory] = mapped_column(Enum(PerkCategory, name="perk_category"), nullable=False)
    cost_coins: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", _jsonb(), default=dict, nullable=False)

    translations: Mapped[list["PerkTranslation"]] = relationship(back_populates="perk", cascade="all, delete-orphan")

    __table_args__ = (CheckConstraint("cost_coins > 0", name="ck_perk_cost_pos"),)


class PerkTranslation(Base):
    __tablename__ = "perk_translations"

    perk_id: Mapped[int] = mapped_column(ForeignKey("perks.perk_id", ondelete="CASCADE"), primary_key=True)
    language: Mapped[Language] = mapped_column(Enum(Language, name="language_code"), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    perk: Mapped[Perk] = relationship(back_populates="translations")


class CoinTransaction(Base):
    __tablename__ = "coin_transactions"

    txn_id: Mapped[int] = mapped_column(_bigpk(), primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id"), index=True, nullable=False)
    type: Mapped[TxnType] = mapped_column(Enum(TxnType, name="txn_type"), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    perk_id: Mapped[Optional[int]] = mapped_column(ForeignKey("perks.perk_id"), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class UserPerk(Base):
    __tablename__ = "user_perks"

    user_perk_id: Mapped[int] = mapped_column(_bigpk(), primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), index=True, nullable=False)
    perk_id: Mapped[int] = mapped_column(ForeignKey("perks.perk_id"), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    uses_left: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_txn_id: Mapped[Optional[int]] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), ForeignKey("coin_transactions.txn_id"), nullable=True)

    user: Mapped[User] = relationship(back_populates="perks")
    perk: Mapped[Perk] = relationship()


class DailyEarnCap(Base):
    __tablename__ = "daily_earn_caps"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    earn_date: Mapped[date] = mapped_column(Date, primary_key=True)
    earned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class Event(Base):
    __tablename__ = "events"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reward_coins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", _jsonb(), default=dict, nullable=False)


class EventParticipation(Base):
    __tablename__ = "event_participation"

    event_id: Mapped[int] = mapped_column(ForeignKey("events.event_id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ModerationAction(Base):
    __tablename__ = "moderation_actions"

    action_id: Mapped[int] = mapped_column(_bigpk(), primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id"), index=True, nullable=False)
    moderator_id: Mapped[Optional[uuid.UUID]] = mapped_column(GUID(), ForeignKey("users.user_id"), nullable=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ClipShare(Base):
    """Tracks Day/Night GIF shares for the +5 coin reward with a daily cap."""
    __tablename__ = "clip_shares"

    share_id: Mapped[int] = mapped_column(_bigpk(), primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), index=True, nullable=False)
    cycle: Mapped[str] = mapped_column(String(8), nullable=False)  # 'day' or 'night'
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    __table_args__ = (CheckConstraint("cycle in ('day','night')", name="ck_clip_cycle"),)


# ================= Game stats / achievements =================

class UserStats(Base):
    __tablename__ = "user_stats"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    games_played: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deaths: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    town_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mafia_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    maniac_wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_mafia: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_don: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_sheriff: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_doctor: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_lover: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_maniac: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    as_civilian: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_game_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class GameRecord(Base):
    __tablename__ = "game_records"

    game_id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    winner: Mapped[str] = mapped_column(String(16), nullable=False)     # town|mafia|maniac
    round_count: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    players_count: Mapped[int] = mapped_column(Integer, nullable=False)
    ended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)


class GameParticipant(Base):
    __tablename__ = "game_participants"

    game_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("game_records.game_id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    survived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    won: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    elo_delta: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class UserAchievement(Base):
    __tablename__ = "user_achievements"

    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
