"""Document: an uploaded file (geometry, land title, permit, certificate...)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.enums import DocumentKind

if TYPE_CHECKING:
    from app.models.plot import Plot
    from app.models.supplier import Supplier


class Document(TimestampMixin, Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int | None] = mapped_column(
        ForeignKey("supplier.id", ondelete="CASCADE"), index=True
    )
    plot_id: Mapped[int | None] = mapped_column(
        ForeignKey("plot.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DocumentKind] = mapped_column(
        SAEnum(DocumentKind, native_enum=False, length=16), default=DocumentKind.other
    )
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(128))
    storage_path: Mapped[str] = mapped_column(String(1024))  # path in the evidence store
    sha256: Mapped[str | None] = mapped_column(String(64))
    extracted: Mapped[str | None] = mapped_column(Text)  # JSON extraction result

    supplier: Mapped[Supplier | None] = relationship(back_populates="documents")
    plot: Mapped[Plot | None] = relationship(back_populates="documents")

    def __repr__(self) -> str:
        return f"<Document id={self.id} kind={self.kind.value} filename={self.filename!r}>"
