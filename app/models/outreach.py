"""OutreachMessage: one message in a supplier data-collection thread."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import OutreachStatus

if TYPE_CHECKING:
    from app.models.supplier import Supplier


class OutreachMessage(TimestampMixin, Base):
    __tablename__ = "outreach_message"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(16), default="email")  # email | whatsapp
    direction: Mapped[str] = mapped_column(String(8), default="out")  # out | in
    status: Mapped[OutreachStatus] = mapped_column(
        SAEnum(OutreachStatus, native_enum=False, length=16), default=OutreachStatus.pending
    )
    subject: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    supplier: Mapped[Supplier] = relationship(back_populates="outreach_messages")

    def __repr__(self) -> str:
        return f"<OutreachMessage id={self.id} status={self.status.value}>"
