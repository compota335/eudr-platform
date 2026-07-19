"""Plot: a parcel of land where a commodity is produced.

Geometry is stored as a GeoJSON geometry object (WGS84 / EPSG:4326),
serialised to text. Spatial operations are performed in-process with Shapely,
which keeps the schema portable across SQLite (dev) and PostgreSQL (prod)
without requiring a PostGIS build. Centroid lon/lat are denormalised for cheap
listing and map rendering.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import Commodity, PlotStatus, RiskLevel

if TYPE_CHECKING:
    from app.models.alert import Alert
    from app.models.document import Document
    from app.models.evidence import Evidence
    from app.models.supplier import Supplier


class Plot(TimestampMixin, Base):
    __tablename__ = "plot"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        ForeignKey("supplier.id", ondelete="CASCADE"), index=True
    )
    external_ref: Mapped[str | None] = mapped_column(String(128))  # supplier's parcel id
    commodity: Mapped[Commodity | None] = mapped_column(
        SAEnum(Commodity, native_enum=False, length=16)
    )
    country: Mapped[str | None] = mapped_column(String(2))  # declared production country

    geometry_geojson: Mapped[str] = mapped_column(Text)  # GeoJSON geometry, WGS84
    geometry_type: Mapped[str] = mapped_column(String(24))  # Point | Polygon | MultiPolygon
    area_ha: Mapped[float | None] = mapped_column(Float)
    centroid_lon: Mapped[float | None] = mapped_column(Float)
    centroid_lat: Mapped[float | None] = mapped_column(Float)

    status: Mapped[PlotStatus] = mapped_column(
        SAEnum(PlotStatus, native_enum=False, length=16), default=PlotStatus.pending
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    risk_level: Mapped[RiskLevel | None] = mapped_column(
        SAEnum(RiskLevel, native_enum=False, length=8)
    )

    supplier: Mapped[Supplier] = relationship(back_populates="plots")
    documents: Mapped[list[Document]] = relationship(
        back_populates="plot", cascade="all, delete-orphan"
    )
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="plot", cascade="all, delete-orphan"
    )
    alerts: Mapped[list[Alert]] = relationship(
        back_populates="plot", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Plot id={self.id} type={self.geometry_type} status={self.status.value}>"
