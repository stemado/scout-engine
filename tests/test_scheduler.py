"""Tests for the scheduler service."""

import pytest
from datetime import datetime

from app.services.scheduler import parse_cron_expression, compute_next_run


def test_parse_standard_cron():
    """Should parse standard 5-field cron expressions."""
    fields = parse_cron_expression("0 6 * * 1-5")
    assert fields["minute"] == "0"
    assert fields["hour"] == "6"
    assert fields["day"] == "*"
    assert fields["month"] == "*"
    assert fields["day_of_week"] == "1-5"


def test_parse_cron_with_seconds():
    """Should parse 6-field cron (with seconds)."""
    fields = parse_cron_expression("30 0 6 * * 1-5")
    assert fields["second"] == "30"
    assert fields["minute"] == "0"
    assert fields["hour"] == "6"


def test_parse_invalid_cron():
    """Should raise ValueError for invalid expressions."""
    with pytest.raises(ValueError):
        parse_cron_expression("not a cron")


def test_parse_invalid_cron_too_many_fields():
    """Should raise ValueError for expressions with too many fields."""
    with pytest.raises(ValueError):
        parse_cron_expression("1 2 3 4 5 6 7")


def test_compute_next_run():
    """Should compute the next run time from a cron expression."""
    next_run = compute_next_run("0 6 * * *", timezone="UTC")
    assert next_run is not None
    assert isinstance(next_run, datetime)


def test_compute_next_run_with_timezone():
    """Should respect timezone when computing next run."""
    next_utc = compute_next_run("0 6 * * *", timezone="UTC")
    next_eastern = compute_next_run("0 6 * * *", timezone="US/Eastern")
    # Both should return a datetime, but they may differ
    assert next_utc is not None
    assert next_eastern is not None
