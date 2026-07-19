"""Evidence: an append-only artifact produced by a pipeline stage for a plot.

Deforestation analysis, geometry checks and legality extraction each write an
Evidence row. Rows are never mutated in place: a re-check writes a new row, so
the audit trail is complete for the 5-year retention obligation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import RiskLevel

if TYPE_CHECKING:
    from app.models.plot import Plot


class Evidence(TimestampMixin, Base):
    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    plot_id: Mapped[int] = mapped_column(
        ForeignKey("plot.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(64))  # e.g. "deforestation_analysis"
    provider: Mapped[str | None] = mapped_column(String(64))  # whisp | gfw | internal
    risk_level: Mapped[RiskLevel | None] = mapped_column(
        SAEnum(RiskLevel, native_enum=False, length=8)
    )
    data_json: Mapped[str] = mapped_column(Text)  # provider response / computed stats
    dataset_versions: Mapped[str | None] = mapped_column(Text)  # JSON: dataset versions/dates
    storage_path: Mapped[str | None] = mapped_column(String(1024))  # thumbnails etc.

    plot: Mapped[Plot] = relationship(back_populates="evidence")

    def __repr__(self) -> str:
        return f"<Evidence id={self.id} stage={self.stage!r} plot_id={self.plot_id}>"
