"""The performance benchmark tool (`app.bench`): its statistics, its inputs and its report.

No models and no browser here: the numbers it produces are measured by `python -m app.bench`.
These tests check the parts that decide whether those numbers can be trusted.
"""

import json
import math
from pathlib import Path
from typing import Any

import pytest

from app.bench import data, report
from app.bench.stats import percentile, repeat_ms, summarize, timed
from app.bench.suites import database

# ------------------------------------------------------------------ statistics


def test_percentile_uses_linear_interpolation_between_closest_ranks() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10.0
    assert percentile(values, 50) == 30.0
    assert percentile(values, 100) == 50.0
    assert percentile(values, 25) == 20.0
    assert percentile(values, 95) == pytest.approx(48.0)  # rank 3.8 between 40 and 50
    assert percentile([7.0], 99) == 7.0
    assert math.isnan(percentile([], 50))


def test_summarize_reports_n_and_the_usual_percentiles_and_ignores_nan() -> None:
    s = summarize([float(i) for i in range(1, 101)] + [math.nan])
    assert s["n"] == 100 and s["min"] == 1.0 and s["max"] == 100.0
    assert s["p50"] == pytest.approx(50.5) and s["p95"] == pytest.approx(95.05)
    assert s["mean"] == pytest.approx(50.5)
    json.dumps(s, allow_nan=False)


def test_summarize_of_nothing_is_nulls_not_nan() -> None:
    s = summarize([])
    assert s["n"] == 0 and s["p95"] is None
    json.dumps(s, allow_nan=False)


def test_timed_and_repeat_ms_count_only_the_calls_after_the_warmup() -> None:
    calls: list[int] = []
    samples = repeat_ms(lambda: calls.append(1), 5, warmup=3)
    assert len(calls) == 8 and len(samples) == 5 and all(x >= 0 for x in samples)
    result, ms = timed(lambda: 41 + 1)
    assert result == 42 and ms >= 0


# ------------------------------------------------------------------ inputs are reproducible


def test_captions_are_deterministic_and_mixed() -> None:
    a, b = data.captions(60, seed=3), data.captions(60, seed=3)
    assert a == b and a != data.captions(60, seed=4)
    text = " ".join(c for c, _ in a)
    assert any("ऀ" <= ch <= "ॿ" for ch in text)  # Devanagari is in the mix
    assert all(1 <= len(tags) <= 4 and all(f"#{t}" in c for t in tags) for c, tags in a)


def test_frames_are_valid_deterministic_jpegs_of_the_stated_size() -> None:
    from io import BytesIO

    from PIL import Image

    one, again = data.make_frame(5), data.make_frame(5)
    assert one == again and one != data.make_frame(6)
    image = Image.open(BytesIO(one))
    assert image.format == "JPEG" and image.size == (data.WIDTH, data.HEIGHT)
    assert one.startswith(b"\xff\xd8") and one.rstrip(b"\x00").endswith(b"\xff\xd9")
    assert 20_000 < len(one) < 200_000  # a photo-sized frame, under the server's 150 KB cap


def test_frames_can_come_from_a_folder_and_a_missing_folder_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "a.jpg").write_bytes(data.make_frame(1))
    (tmp_path / "b.JPEG").write_bytes(data.make_frame(2))
    (tmp_path / "ignored.txt").write_text("x")
    frames = data.load_frames(tmp_path, 5)
    assert len(frames) == 5 and frames[0] == frames[2]  # two files, cycled
    with pytest.raises(FileNotFoundError):
        data.load_frames(tmp_path / "empty-dir-that-does-not-exist", 1)


# ------------------------------------------------------------------ the database is only wiped if it says so


def test_the_benchmark_refuses_a_database_that_is_not_named_bench() -> None:
    for url in (
        "postgresql+psycopg://u:p@localhost/ref",
        "postgresql+psycopg://u:p@localhost/ref_test",
        "postgresql+psycopg://u:p@localhost/postgres",
        "postgresql+psycopg://u:p@localhost/",
    ):
        with pytest.raises(database.UnsafeDatabase):
            database.check_name(url)
        with pytest.raises(database.UnsafeDatabase):
            database.engine_for(url)
    database.check_name("postgresql+psycopg://u:p@localhost/ref_bench")  # accepted


# ------------------------------------------------------------------ the report


def interval(p50: float, p95: float, n: int = 50) -> dict[str, Any]:
    return {
        "n": n,
        "min": p50 / 2,
        "mean": p50,
        "p50": p50,
        "p90": p95,
        "p95": p95,
        "p99": p95,
        "max": p95 * 1.1,
    }


def browser(detection_p95: float, mutation_p95: float, over: int = 0) -> dict[str, Any]:
    def scenario(slowdown: int) -> dict[str, Any]:
        det = {
            "from_60_percent_ms": interval(5, detection_p95),
            "from_scroll_command_ms": interval(20, 90),
            "missed": 0,
        }
        frames = {
            "frames": 500,
            "frame_ms": interval(16, 17),
            "frames_over_20ms": 4,
            "frames_over_33ms": 2,
            "frames_over_50ms": 0,
            "long_tasks": 1,
            "longest_task_ms": 60,
        }
        return {
            "cpu_slowdown": slowdown,
            "dom_elements": 46000,
            "detection_smooth_scroll": det,
            "detection_instant_jump": det,
            "read_reel_metadata_ms": interval(3, 4),
            "intersection_callback_ms": interval(2, 4),
            "intersection_callbacks_over_5ms": over,
            "mutation_batches": {
                "1_reels_added_per_batch": {
                    "mutation_callback_ms": interval(1, mutation_p95),
                    "over_5ms": 0,
                }
            },
            "scroll_without_tracker": frames,
            "scroll_with_tracker": frames,
        }

    return {
        "suite": "browser",
        "environment": {"chromium": "1.0", "viewport": "1280x800"},
        "page": {"reels": 60, "extra_elements_per_reel": 150},
        "scenarios": [scenario(1), scenario(4)],
    }


def pipeline(seconds_per_reel: float, reels: int = 200) -> dict[str, Any]:
    wall = seconds_per_reel * reels
    return {
        "suite": "pipeline",
        "environment": {},
        "result": {
            "reels": reels,
            "workers": 1,
            "threads_per_worker": 4,
            "visual_model": "siglip2-base",
            "ocr": "devanagari",
            "frames_per_reel": 5,
            "ocr_frames_per_reel": 2,
            "wall_seconds_for_all_reels": wall,
            "seconds_per_reel_wall": seconds_per_reel,
            "reels_per_second": 1 / seconds_per_reel,
            "fingerprint_rebuild_seconds": 0.1,
            "prediction_rebuild_seconds": 0.2,
            "total_with_post_seconds": wall + 0.3,
            "per_reel_total_ms": interval(seconds_per_reel * 1000, seconds_per_reel * 1100),
            "per_reel_ocr_ms": interval(1200, 1300),
            "per_reel_process_ms": interval(900, 1000),
            "stage_ms": {"embed_frames": interval(800, 900), "fuse": interval(0.1, 0.2)},
            "failed_stages": [],
            "peak_rss_mb_per_worker": [600.0],
            "cpu_ids_per_worker": [None],
        },
    }


def _full(d: dict[str, Any]) -> dict[str, Any]:
    """The shape `report.collect` produces: results grouped by kind, with `browser` as an alias."""
    full = dict(d)
    if d.get("browser") is not None:
        full["browsers"] = {"browser": d["browser"]}
    return full


def _render(d: dict[str, Any]) -> str:
    return report.render(_full(d))


def verdicts(d: dict[str, Any]) -> dict[str, str]:
    return {row[0]: row[3] for row in report.summary_rows(_full(d))}


def test_a_target_met_is_reported_met_and_a_target_missed_is_reported_missed() -> None:
    good = {"browser": browser(10, 2), "pipelines": {"fast": pipeline(2.2)}}
    v = verdicts(good)
    assert v["N1 Reel detection latency"] == "**met**"
    assert v["N2 content-script work per mutation batch"] == "**met**"
    assert v["N3 worker throughput: `fast`"] == "**met**"
    assert v["N3 200-Reel session: `fast`"] == "**met**"

    bad = {"browser": browser(600, 7), "pipelines": {"slow": pipeline(3.4)}}
    v = verdicts(bad)
    assert v["N1 Reel detection latency"] == "**MISSED**"
    assert v["N2 content-script work per mutation batch"] == "**MISSED**"
    assert v["N3 worker throughput: `slow`"] == "**MISSED**"
    assert v["N3 200-Reel session: `slow`"] == "**MISSED**"


def test_the_boundaries_are_exclusive_or_inclusive_as_the_spec_says() -> None:
    # "< 500 ms" and "< 5 ms" are strict; "at least 1 Reel per 3 s" and "within 10 minutes" are not.
    assert (
        verdicts({"browser": browser(500, 4), "pipelines": {}})["N1 Reel detection latency"]
        == "**MISSED**"
    )
    assert (
        verdicts({"browser": browser(499.9, 4.99), "pipelines": {}})["N1 Reel detection latency"]
        == "**met**"
    )
    v = verdicts({"pipelines": {"edge": pipeline(3.0)}})
    assert (
        v["N3 worker throughput: `edge`"] == "**met**"
        and v["N3 200-Reel session: `edge`"] == "**met**"
    )
    assert (
        verdicts({"pipelines": {"over": pipeline(3.0001)}})["N3 worker throughput: `over`"]
        == "**MISSED**"
    )


def test_fewer_than_200_reels_is_never_reported_as_the_200_reel_result() -> None:
    v = verdicts({"pipelines": {"short": pipeline(1.0, reels=50)}})
    assert v["N3 50-Reel session: `short`"] == "not 200 Reels"


def test_nothing_measured_is_not_reported_as_met() -> None:
    assert report.summary_rows({"pipelines": {}}) == []
    text = _render({"pipelines": {}, "analysis": None})
    assert "**met**" not in text and "Summary against the targets" in text


def test_the_report_names_what_it_did_not_measure() -> None:
    text = _render(
        {"browser": browser(10, 2), "pipelines": {"fast": pipeline(2.2)}, "analysis": None}
    )
    assert "Nothing here was measured on Instagram or on real Reel frames" in text
    assert "frame capture (M4) is not built" in text and "not measured" in text
    assert "The worker is composed by the benchmark" in text


def test_the_report_loads_result_files_by_suite_and_includes_the_analysis(tmp_path: Path) -> None:
    (tmp_path / "browser.json").write_text(json.dumps(browser(10, 2)))
    (tmp_path / "pipeline-a.json").write_text(json.dumps(pipeline(2.0)))
    (tmp_path / "pipeline-b.json").write_text(json.dumps(pipeline(2.5)))
    (tmp_path / "unrelated.json").write_text(json.dumps({"suite": "other"}))
    (tmp_path / "analysis.md").write_text("## Bottlenecks\n\nOCR dominates.\n")
    loaded = report.load(tmp_path)
    assert set(loaded["pipelines"]) == {"pipeline-a", "pipeline-b"} and "browser" in loaded
    text = _render(loaded)
    assert "## Bottlenecks" in text and "OCR dominates." in text
    assert "`pipeline-a`" in text and "`pipeline-b`" in text


def test_every_number_comes_from_the_files_not_from_the_code() -> None:
    a = _render({"browser": browser(10, 2), "pipelines": {"x": pipeline(2.0)}, "analysis": None})
    b = _render({"browser": browser(77, 3), "pipelines": {"x": pipeline(2.9)}, "analysis": None})
    assert a != b and "2.90 s per Reel" in b and "2.00 s per Reel" in a
    assert "p95 77.0 ms from 60% visible" in b
