"""Supplier: a producer / cooperative that supplies a client."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import Commodity, OutreachStatus

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.document import Document
    from app.models.outreach import OutreachMessage
    from app.models.plot import Plot


class Supplier(TimestampMixin, Base):
    __tablename__ = "supplier"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(2))
    commodity: Mapped[Commodity | None] = mapped_column(
        SAEnum(Commodity, native_enum=False, length=16)
    )
    contact_email: Mapped[str | None] = mapped_column(String(320))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    language: Mapped[str | None] = mapped_column(String(8))  # outreach language, e.g. "es"
    approx_volume_t: Mapped[float | None] = mapped_column(Float)  # declared annual tonnes
    magic_token: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    outreach_status: Mapped[OutreachStatus] = mapped_column(
        SAEnum(OutreachStatus, native_enum=False, length=16),
        default=OutreachStatus.pending,
    )

    client: Mapped[Client] = relationship(back_populates="suppliers")
    plots: Mapped[list[Plot]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )
    outreach_messages: Mapped[list[OutreachMessage]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Supplier id={self.id} name={self.name!r} country={self.country}>"
