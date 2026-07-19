"""PlotCheck: an anonymous public plot-checker submission (the lead magnet).

Deliberately separate from the ``Supplier -> Plot`` pipeline. The public
checker accepts uploads with no account, so persisting them as ``Plot`` rows
would mean fabricating ``Client``/``Supplier`` records for every visitor and
polluting the CRM. A ``PlotCheck`` is self-contained; the visitor's email is
captured only when they request the gated PDF export.

The row is addressed publicly by an opaque ``token`` (never the sequential
primary key), so one visitor cannot enumerate another's check.
"""

from __future__ import annotations

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import Commodity, RiskLevel


class PlotCheck(TimestampMixin, Base):
    __tablename__ = "plot_check"

    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(43), unique=True, index=True)

    source_format: Mapped[str] = mapped_column(String(16))
    geometry_geojson: Mapped[str] = mapped_column(Text)  # GeoJSON geometry, WGS84
    geometry_type: Mapped[str] = mapped_column(String(24))
    area_ha: Mapped[float | None] = mapped_column(Float)
    centroid_lon: Mapped[float | None] = mapped_column(Float)
    centroid_lat: Mapped[float | None] = mapped_column(Float)

    commodity: Mapped[Commodity | None] = mapped_column(
        SAEnum(Commodity, native_enum=False, length=16)
    )
    country: Mapped[str | None] = mapped_column(String(2))

    risk_level: Mapped[RiskLevel | None] = mapped_column(
        SAEnum(RiskLevel, native_enum=False, length=8)
    )
    ruleset_version: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(32))
    result_json: Mapped[str | None] = mapped_column(Text)  # full RiskResult + evidence
    dataset_versions: Mapped[str | None] = mapped_column(Text)  # JSON: dataset versions

    email: Mapped[str | None] = mapped_column(String(320), index=True)  # captured at PDF gate

    def __repr__(self) -> str:
        return f"<PlotCheck id={self.id} token={self.token!r} risk={self.risk_level}>"
