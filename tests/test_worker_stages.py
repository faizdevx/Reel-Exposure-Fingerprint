"""Stage runner tests: every stage reports started_at, finished_at, duration, status and error."""

import logging
from datetime import UTC, datetime

import pytest
from worker_fakes import FakeClock

from app.worker.stages import StageRunner, StageSkipped

SECRET = "a private caption that must never appear in a report or a log"


def test_a_successful_stage_reports_times_duration_and_status() -> None:
    clock = FakeClock()
    runner = StageRunner(clock.clocks())

    def work() -> int:
        clock.advance(1.5)
        return 42

    outcome = runner.run("embed_text", work)
    assert outcome.ok
    assert outcome.value == 42
    report = outcome.report
    assert report.stage == "embed_text"
    assert report.status == "ok"
    assert report.started_at == datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    assert report.finished_at == datetime(2026, 10, 7, 12, 0, 1, 500000, tzinfo=UTC)
    assert report.duration_ms == pytest.approx(1500.0)
    assert report.error is None
    assert report.reason is None
    assert runner.reports == [report]


def test_a_failed_stage_reports_the_error_class_only() -> None:
    runner = StageRunner(FakeClock().clocks())

    def boom() -> None:
        raise ValueError(SECRET)

    outcome = runner.run("score_topics", boom)
    assert not outcome.ok
    assert outcome.value is None
    assert outcome.report.status == "failed"
    assert outcome.report.error == "ValueError"
    assert SECRET not in repr(outcome.report)
    assert SECRET not in str(outcome.report.as_dict())


def test_a_failing_stage_does_not_stop_the_next_one() -> None:
    runner = StageRunner()

    def boom() -> None:
        raise RuntimeError("x")

    runner.run("a", boom)
    later = runner.run("b", lambda: "fine")
    assert later.ok
    assert [r.status for r in runner.reports] == ["failed", "ok"]


def test_a_skipped_stage_carries_its_reason_and_no_error() -> None:
    runner = StageRunner()

    def nothing() -> None:
        raise StageSkipped("no_text")

    outcome = runner.run("normalize", nothing)
    assert outcome.report.status == "skipped"
    assert outcome.report.reason == "no_text"
    assert outcome.report.error is None
    assert not outcome.ok


def test_skip_records_a_stage_that_was_not_attempted() -> None:
    clock = FakeClock()
    runner = StageRunner(clock.clocks())
    runner.skip("embed_text", "upstream_normalize")
    report = runner.reports[0]
    assert (report.status, report.reason, report.error) == ("skipped", "upstream_normalize", None)
    assert report.duration_ms == 0.0
    assert report.started_at == report.finished_at


def test_reports_keep_the_order_the_stages_ran_in() -> None:
    runner = StageRunner()
    for name in ("one", "two", "three"):
        runner.run(name, lambda: None)
    runner.skip("four", "r")
    assert [r.stage for r in runner.reports] == ["one", "two", "three", "four"]
    assert runner.status_of("two") == "ok"
    assert runner.status_of("four") == "skipped"
    assert runner.status_of("missing") is None


def test_duration_comes_from_the_monotonic_clock_not_the_wall_clock() -> None:
    clock = FakeClock()
    runner = StageRunner(clock.clocks())

    def work() -> None:
        clock.mono += 0.25
        clock.wall_time = datetime(2020, 1, 1, tzinfo=UTC)  # the wall clock jumps backwards

    report = runner.run("s", work).report
    assert report.duration_ms == pytest.approx(250.0)
    assert report.duration_ms >= 0


def test_duration_is_never_negative() -> None:
    clock = FakeClock()
    runner = StageRunner(clock.clocks())

    def work() -> None:
        clock.mono -= 5.0

    assert runner.run("s", work).report.duration_ms == 0.0


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_interrupts_are_not_swallowed(signal: type[BaseException]) -> None:
    runner = StageRunner()

    def stop() -> None:
        raise signal

    with pytest.raises(signal):
        runner.run("s", stop)


def test_report_as_dict_is_json_friendly() -> None:
    runner = StageRunner(FakeClock().clocks())
    report = runner.run("s", lambda: None).report
    data = report.as_dict()
    assert set(data) == {
        "stage",
        "status",
        "started_at",
        "finished_at",
        "duration_ms",
        "error",
        "reason",
    }
    assert data["started_at"] == "2026-10-07T12:00:00+00:00"


def test_logs_hold_stage_status_and_error_class_but_never_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    runner = StageRunner()

    def boom() -> None:
        raise ValueError(SECRET)

    runner.run("embed_text", boom)
    assert "stage=embed_text status=failed" in caplog.text
    assert "error=ValueError" in caplog.text
    assert SECRET not in caplog.text
