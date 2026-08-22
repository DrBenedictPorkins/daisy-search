"""Constants back the immutability and lifecycle guarantees the README claims."""

from daisy import constants as c


def test_lifecycle_states_cover_the_documented_pipeline():
    assert set(c.LIFECYCLE_STATES) == {
        "unconfirmed",
        "likely",
        "confirmed",
        "discarded",
        "needs_revalidation",
    }


def test_new_facts_start_unconfirmed():
    assert c.DEFAULT_LIFECYCLE_STATE == c.LifecycleState.UNCONFIRMED
    assert c.DEFAULT_FACT_CONFIDENCE == 0.0


def test_projects_default_to_active():
    assert c.DEFAULT_PROJECT_STATUS == c.ProjectStatus.ACTIVE
    assert c.DEFAULT_PROJECT_STATUS in c.PROJECT_STATUSES


def test_model_ids_use_aliases_not_dated_versions():
    """Convention: aliases only, so upgrades don't require a code change."""
    for model in (
        c.DEFAULT_EXTRACTION_MODEL,
        c.DEFAULT_RESOLUTION_MODEL,
        c.DEFAULT_TRIAGE_MODEL,
    ):
        assert model.startswith("claude-")
        assert not model.split("-")[-1].isdigit() or len(model.split("-")[-1]) != 8
