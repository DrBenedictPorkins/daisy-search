"""SQLAlchemy ORM models."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


# Import all models so Alembic can detect them
from daisy.models.fact import Fact, FactEvent  # noqa: E402, F401
from daisy.models.job import Job  # noqa: E402, F401
from daisy.models.project import Project  # noqa: E402, F401
from daisy.models.source import Source  # noqa: E402, F401
from daisy.models.subject import Subject, SubjectAlias  # noqa: E402, F401
