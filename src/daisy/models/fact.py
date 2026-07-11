"""Fact and fact-event models."""

import uuid
from datetime import date

from sqlalchemy import CheckConstraint, Date, Float, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from daisy.constants import (
    DEFAULT_FACT_CONFIDENCE,
    DEFAULT_LIFECYCLE_STATE,
    DEFAULT_RESOLUTION_SCORE,
    LIFECYCLE_STATES,
)
from daisy.models import Base


class Fact(Base):
    """An immutable claim extracted from a source."""

    __tablename__ = "facts"
    __table_args__ = (
        CheckConstraint(
            f"lifecycle_state IN {LIFECYCLE_STATES!r}",
            name="ck_facts_lifecycle_state",
        ),
    )

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
    subject_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
    )
    claim_text: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(Text, nullable=False)
    raw_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    extraction_date: Mapped[uuid.UUID] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    local_confidence: Mapped[float] = mapped_column(
        Float, default=DEFAULT_FACT_CONFIDENCE, nullable=False
    )
    resolution_score: Mapped[float] = mapped_column(
        Float, default=DEFAULT_RESOLUTION_SCORE, nullable=False
    )
    lifecycle_state: Mapped[str] = mapped_column(
        Text,
        default=DEFAULT_LIFECYCLE_STATE,
        server_default=DEFAULT_LIFECYCLE_STATE,
        nullable=False,
    )
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[uuid.UUID] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    project: Mapped["Project"] = relationship(
        back_populates="facts",
    )
    events: Mapped[list["FactEvent"]] = relationship(
        back_populates="fact", cascade="all, delete-orphan"
    )


# Avoid circular import for type hints
from daisy.models.project import Project  # noqa: E402, F401


class FactEvent(Base):
    """Audit log entry for a fact lifecycle state transition."""

    __tablename__ = "fact_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    fact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("facts.id", ondelete="CASCADE"),
        nullable=False,
    )
    old_state: Mapped[str] = mapped_column(Text, nullable=False)
    new_state: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=True,
    )
    created_at: Mapped[uuid.UUID] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    fact: Mapped["Fact"] = relationship(back_populates="events")
