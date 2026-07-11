"""Shared pytest fixtures for the Daisy Search test suite."""

import pytest


@pytest.fixture
def sample_subject_data() -> dict:
    """Return sample input data for creating a subject."""
    return {
        "name": "Test Subject",
        "notes": "Test subject for unit tests",
    }


@pytest.fixture
def sample_intake_input() -> dict:
    """Return sample input data for an intake job."""
    return {
        "name": "Test Subject",
        "known_aliases": ["tsubject", "testsubject"],
    }
