"""Project model — top-level container for research investigations."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from daisy.constants import DEFAULT_PROJECT_STATUS, PROJECT_STATUSES
from daisy.models import Base


class Project(Base):
    """A research investigation containing subjects, facts, and sources."""

    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            f"status IN {PROJECT_STATUSES!r}",
            name="ck_projects_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
    status: Mapped[str] = mapped_column(
        Text,
        default=DEFAULT_PROJECT_STATUS,
        server_default=DEFAULT_PROJECT_STATUS,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    workspace_path: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    subjects: Mapped[list["Subject"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )
    facts: Mapped[list["Fact"]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
    )


# Avoid circular import — these are used for type hints only
from daisy.models.fact import Fact  # noqa: E402, F401
from daisy.models.subject import Subject  # noqa: E402, F401
