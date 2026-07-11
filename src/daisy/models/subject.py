"""Subject and alias models."""

import uuid

from sqlalchemy import Float, ForeignKey, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from daisy.constants import DEFAULT_ALIAS_CONFIDENCE
from daisy.models import Base


class Subject(Base):
    """A person being researched."""

    __tablename__ = "subjects"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[uuid.UUID] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    project: Mapped["Project"] = relationship(
        back_populates="subjects",
    )
    aliases: Mapped[list["SubjectAlias"]] = relationship(
        back_populates="subject", cascade="all, delete-orphan"
    )


# Avoid circular import for type hints
from daisy.models.project import Project  # noqa: E402, F401


class SubjectAlias(Base):
    """An alternative identifier for a subject (email, username, phone, etc.)."""

    __tablename__ = "subject_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    subject_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="CASCADE"),
        nullable=False,
    )
    alias_type: Mapped[str] = mapped_column(Text, nullable=False)
    alias_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(
        Float, default=DEFAULT_ALIAS_CONFIDENCE, nullable=False
    )
    created_at: Mapped[uuid.UUID] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    subject: Mapped["Subject"] = relationship(back_populates="aliases")
