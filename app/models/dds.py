"""DDS: a Due Diligence Statement assembled for a client."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import DDSStatus

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.shipment import Shipment


class DDS(TimestampMixin, Base):
    __tablename__ = "dds"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(
        ForeignKey("client.id", ondelete="CASCADE"), index=True
    )
    shipment_id: Mapped[int | None] = mapped_column(
        ForeignKey("shipment.id", ondelete="SET NULL"), index=True
    )
    # Internal reference generated at assembly time (distinct from the TRACES
    # reference/verification numbers returned only on successful submission).
    reference_number: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    verification_number: Mapped[str | None] = mapped_column(String(64))
    traces_reference: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[DDSStatus] = mapped_column(
        SAEnum(DDSStatus, native_enum=False, length=16), default=DDSStatus.draft
    )
    payload_json: Mapped[str | None] = mapped_column(Text)  # assembled DDS JSON
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client] = relationship(back_populates="statements")
    shipment: Mapped[Shipment | None] = relationship(back_populates="statements")

    def __repr__(self) -> str:
        return f"<DDS id={self.id} ref={self.reference_number!r} status={self.status.value}>"
