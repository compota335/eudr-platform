"""Shipment: a consignment / batch mapped to the plots that produced it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Column, Float, ForeignKey, String, Table
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import Commodity, ShipmentStatus

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.dds import DDS
    from app.models.plot import Plot


# Association table: a shipment references many plots; a plot can recur across
# shipments (the reuse mechanic once a plot is onboarded and verified).
shipment_plot = Table(
    "shipment_plot",
    Base.metadata,
    Column("shipment_id", ForeignKey("shipment.id", ondelete="CASCADE"), primary_key=True),
    Column("plot_id", ForeignKey("plot.id", ondelete="CASCADE"), primary_key=True),
)


class Shipment(TimestampMixin, Base):
    __tablename__ = "shipment"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), index=True
    )
    reference: Mapped[str | None] = mapped_column(String(128))  # client's shipment ref
    commodity: Mapped[Commodity | None] = mapped_column(
        SAEnum(Commodity, native_enum=False, length=16)
    )
    cn_code: Mapped[str | None] = mapped_column(String(16))
    quantity_kg: Mapped[float | None] = mapped_column(Float)
    country_of_production: Mapped[str | None] = mapped_column(String(2))
    status: Mapped[ShipmentStatus] = mapped_column(
        SAEnum(ShipmentStatus, native_enum=False, length=16), default=ShipmentStatus.pending
    )

    client: Mapped[Client] = relationship(back_populates="shipments")
    plots: Mapped[list[Plot]] = relationship(secondary=shipment_plot)
    statements: Mapped[list[DDS]] = relationship(back_populates="shipment")

    def __repr__(self) -> str:
        return f"<Shipment id={self.id} ref={self.reference!r} status={self.status.value}>"
