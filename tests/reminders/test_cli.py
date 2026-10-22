"""Служебная CLI напоминаний: только белый список номеров и никаких ПДн в выводе."""

import json
import os
import subprocess
import sys
from datetime import date

import pytest

from app.reminders.cli import parser, run
from app.reminders.models import ReminderJob
from app.reminders.settings import ReminderSettings
from tests.reminders.test_reminders import PHONE, at, reminder_store  # noqa: F401

SETTINGS = ReminderSettings(_env_file=None)


def args(*argv):
    return parser().parse_args(argv)


async def test_contact_defaults_to_the_team_softphone(reminder_store):  # noqa: F811
    result = await run(args("contact-set", "--client-id", "3"), reminder_store, SETTINGS)
    assert result == {
        "client_id": 3,
        "saved": True,
        "opt_out": False,
        "notification_opt_out": False,
    }
    assert (await reminder_store.contact(3)).endpoint == PHONE
    shown = await run(args("contact-show", "--client-id", "3"), reminder_store, SETTINGS)
    assert shown == {
        "client_id": 3,
        "has_contact": True,
        "endpoint_allowed": True,
        "opt_out_at": None,
        "notification_opt_out_at": None,
    }
    assert PHONE not in json.dumps(shown)


async def test_number_outside_the_allowed_list_is_refused(reminder_store):  # noqa: F811
    with pytest.raises(ValueError, match="REMINDERS_ALLOWED_ENDPOINTS"):
        await run(
            args("contact-set", "--client-id", "3", "--endpoint", "PJSIP/+79990000000@gateway"),
            reminder_store,
            SETTINGS,
        )
    assert await reminder_store.contact(3) is None


async def test_contact_again_keeps_opt_out(reminder_store):  # noqa: F811
    await run(args("contact-set", "--client-id", "3"), reminder_store, SETTINGS)
    await reminder_store.opt_out(3, at(19, 10))
    result = await run(args("contact-set", "--client-id", "3"), reminder_store, SETTINGS)
    assert result["opt_out"] is True


async def test_jobs_show_only_technical_fields(reminder_store):  # noqa: F811
    for visit_id, day in ((7, 19), (8, 19), (9, 20)):
        await reminder_store.add_job(
            ReminderJob(
                visit_id=visit_id,
                client_id=3,
                doctor_id=1,
                scheduled_at=at(day + 1, 10),
                call_day=date(2026, 10, day),
                next_at=at(day, 10),
            )
        )
    result = await run(args("jobs", "--day", "2026-10-19"), reminder_store, SETTINGS)
    assert result["shown"] == 2 and result["states"] == {"planned": 2}
    assert {j["visit_id"] for j in result["jobs"]} == {7, 8}
    assert "client_id" not in result["jobs"][0]
    skipped = await run(args("jobs", "--state", "skipped"), reminder_store, SETTINGS)
    assert skipped["shown"] == 0


def test_cli_help_is_utf8_on_windows():
    env = dict(os.environ)
    env.pop("PYTHONIOENCODING", None)
    result = subprocess.run(
        [sys.executable, "-m", "app.reminders.cli", "--help"],
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "Напоминания С-10" in " ".join(result.stdout.split())
