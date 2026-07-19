"""Alert: a deforestation disturbance signal on a monitored plot."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Date, Float, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import AlertSource

if TYPE_CHECKING:
    from app.models.plot import Plot


class Alert(TimestampMixin, Base):
    __tablename__ = "alert"

    id: Mapped[int] = mapped_column(primary_key=True)
    plot_id: Mapped[int] = mapped_column(
        ForeignKey("plot.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[AlertSource] = mapped_column(SAEnum(AlertSource, native_enum=False, length=8))
    alert_date: Mapped[date | None] = mapped_column(Date)
    confidence: Mapped[str | None] = mapped_column(String(16))
    area_ha: Mapped[float | None] = mapped_column(Float)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    detail_json: Mapped[str | None] = mapped_column(Text)

    plot: Mapped[Plot] = relationship(back_populates="alerts")

    def __repr__(self) -> str:
        return f"<Alert id={self.id} source={self.source.value} date={self.alert_date}>"
