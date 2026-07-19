"""Client: an operator/importer (EU) or exporter (South America) we serve."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import ClientSide

if TYPE_CHECKING:
    from app.models.dds import DDS
    from app.models.shipment import Shipment
    from app.models.supplier import Supplier


class Client(TimestampMixin, Base):
    __tablename__ = "client"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    side: Mapped[ClientSide] = mapped_column(SAEnum(ClientSide, native_enum=False, length=16))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    contact_email: Mapped[str | None] = mapped_column(String(320))
    eori: Mapped[str | None] = mapped_column(String(32))  # EU economic operator id
    notes: Mapped[str | None] = mapped_column(Text)

    suppliers: Mapped[list[Supplier]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    shipments: Mapped[list[Shipment]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )
    statements: Mapped[list[DDS]] = relationship(
        back_populates="client", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Client id={self.id} name={self.name!r} side={self.side.value}>"
