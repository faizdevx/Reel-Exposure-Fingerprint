"""`python -m app.bench report`: docs/performance-report.md from the result files.

Every number in the report is read from `docs/performance/*.json`; the only hand-written text is
the method and its limits (below) and `docs/performance/analysis.md` (bottlenecks and what was
changed), which is included as written. A verdict is computed from the numbers and the spec's
targets, never typed.
"""

import json
from pathlib import Path
from typing import Any

TARGET_DETECTION_MS = 500.0
TARGET_MUTATION_MS = 5.0
TARGET_SECONDS_PER_REEL = 3.0
TARGET_200_REELS_S = 600.0
TARGET_API_P95_MS = 500.0


def load(directory: Path) -> dict[str, Any]:
    found: dict[str, Any] = {"pipelines": {}, "browsers": {}, "profiles": {}, "apis": {}}
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        suite = data.get("suite")
        if suite == "pipeline":
            found["pipelines"][path.stem] = data
        elif suite == "browser":
            found["browsers"][path.stem] = data
        elif path.stem.startswith("profile-read-reel"):
            found["profiles"][path.stem] = data
        elif suite == "api":
            found["apis"][path.stem] = data
        elif suite in ("models", "contention"):
            found[suite] = data
    found["browser"] = found["browsers"].get("browser")
    found["api"] = found["apis"].get("api")
    analysis = directory / "analysis.md"
    found["analysis"] = analysis.read_text(encoding="utf-8").strip() if analysis.exists() else None
    return found


def ms(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:,.{digits}f}"


def verdict(ok: bool | None) -> str:
    return "not measured" if ok is None else ("**met**" if ok else "**MISSED**")


def table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return [*out, ""]


def _p(summary: dict[str, Any] | None, key: str = "p95") -> float | None:
    return None if not summary else summary.get(key)


# ------------------------------------------------------------------ the summary


def _api_rows(a: dict[str, Any], tag: str, *, upload: bool) -> list[list[str]]:
    out: list[list[str]] = []
    if upload:
        up = a["event_upload"]
        out.append(
            [
                "Event upload latency (server round trip)",
                "no number in the spec (F8: sent within 15 s of queueing)",
                f"batch of 1: p95 {ms(_p(up['POST /events, batch of 1']['ms']))} ms; "
                f"batch of 50: p95 {ms(_p(up['POST /events, batch of 50']['ms']))} ms; "
                f"5 frames: p95 {ms(_p(up['POST /events/{id}/frames, 5 frames']['ms']))} ms",
                "no target",
            ]
        )
    endpoints = {k: v for k, v in a["dashboard_api"].items() if not k.startswith("_")}
    worst_seq = max((_p(v["sequential_ms"]) or 0.0, k) for k, v in endpoints.items())
    worst_con = max((_p(v["five_users_at_once_ms"]) or 0.0, k) for k, v in endpoints.items())
    clients = a["dashboard_api"].get("_clients", 5)
    seeded = a.get("seeded_database")
    where = (
        f"{seeded['users']} user(s), {seeded['exposures']:,} exposures"
        if seeded
        else "the database as it was"
    )
    out.append(
        [
            f"N4 dashboard API p95{tag}",
            "< 500 ms",
            f"worst endpoint, one client: {ms(worst_seq[0])} ms ({worst_seq[1]}); {clients} clients at once: "
            f"{ms(worst_con[0])} ms ({worst_con[1]}); {where}",
            verdict(max(worst_seq[0], worst_con[0]) < TARGET_API_P95_MS),
        ]
    )
    return out


def _browser_rows(b: dict[str, Any], tag: str) -> list[list[str]]:
    out: list[list[str]] = []
    worst = max(
        (_p(s["detection_smooth_scroll"]["from_60_percent_ms"]) or 0.0) for s in b["scenarios"]
    )
    slow = next((s for s in b["scenarios"] if s["cpu_slowdown"] == 4), b["scenarios"][-1])
    out.append(
        [
            f"N1 Reel detection latency{tag}",
            "< 500 ms",
            f"p95 {ms(worst)} ms from 60% visible (worst of normal speed and 4x slower CPU; "
            f"4x: p95 {ms(_p(slow['detection_smooth_scroll']['from_60_percent_ms']))} ms)",
            verdict(worst < TARGET_DETECTION_MS),
        ]
    )
    batch_p95 = max(
        (_p(v["mutation_callback_ms"]) or 0.0)
        for s in b["scenarios"]
        for v in s["mutation_batches"].values()
    )
    batch_max = max(
        (v["mutation_callback_ms"]["max"] or 0.0)
        for s in b["scenarios"]
        for v in s["mutation_batches"].values()
    )
    out.append(
        [
            f"N2 content-script work per mutation batch{tag}",
            "< 5 ms",
            f"p95 {ms(batch_p95, 2)} ms, max {ms(batch_max, 2)} ms (worst of both speeds)",
            verdict(batch_p95 < TARGET_MUTATION_MS),
        ]
    )
    base = b["scenarios"][0]
    io = base["intersection_callback_ms"]
    out.append(
        [
            f"N2 work when a Reel becomes primary (observer callback){tag}",
            "< 5 ms (the spec states it for mutation batches; applied here to be safe)",
            f"p50 {ms(_p(io, 'p50'), 2)} ms, p95 {ms(_p(io), 2)} ms at normal speed; "
            f"{base['intersection_callbacks_over_5ms']} of {io['n']} callbacks over 5 ms",
            verdict((_p(io) or 0.0) < TARGET_MUTATION_MS),
        ]
    )
    w, o = base["scroll_without_tracker"], base["scroll_with_tracker"]
    out.append(
        [
            f"N2 dropped scroll frames{tag}",
            "none caused by the extension",
            f"frames over 33 ms: {o['frames_over_33ms']} with the tracker, {w['frames_over_33ms']} without; "
            f"long tasks: {o['long_tasks']} with, {w['long_tasks']} without (normal speed)",
            verdict(o["frames_over_33ms"] <= w["frames_over_33ms"] * 1.1 + 2),
        ]
    )
    out.append(
        [
            f"N2 frame encode off the main path{tag}",
            "off the main path",
            "frame capture (M4) is not built",
            verdict(None),
        ]
    )
    return out


def summary_rows(d: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for stem, b in sorted(d.get("browsers", {}).items(), key=lambda kv: kv[0] != "browser"):
        tag = "" if stem == "browser" else f" [`{stem}`]"
        rows += _browser_rows(b, tag)
    for stem, a in sorted(d.get("apis", {}).items(), key=lambda kv: kv[0] != "api"):
        rows += _api_rows(a, "" if stem == "api" else f" [`{stem}`]", upload=stem == "api")
    for name, p in sorted(d["pipelines"].items()):
        r = p["result"]
        cpus = p.get("environment", {}).get("pinned_cpus")
        tag = (
            "" if cpus in (None, 4) else f" ({cpus} CPUs: a sensitivity run, not the spec's 4 vCPU)"
        )
        ok_rate = r["seconds_per_reel_wall"] <= TARGET_SECONDS_PER_REEL
        ok_200 = r["reels"] >= 200 and r["wall_seconds_for_all_reels"] <= TARGET_200_REELS_S
        rows.append(
            [
                f"N3 worker throughput: `{name}`{tag}",
                "≥ 1 Reel per 3 s",
                f"{ms(r['seconds_per_reel_wall'], 2)} s per Reel ({r['reels_per_second']:.3f} Reels/s)",
                verdict(ok_rate),
            ]
        )
        rows.append(
            [
                f"N3 {r['reels']}-Reel session: `{name}`{tag}",
                "≤ 10 min for 200 Reels",
                f"{ms(r['wall_seconds_for_all_reels'], 1)} s for the Reels, "
                f"{ms(r['total_with_post_seconds'], 1)} s with the fingerprint and prediction rebuild",
                verdict(ok_200) if r["reels"] >= 200 else "not 200 Reels",
            ]
        )
    return rows


# ------------------------------------------------------------------ sections


def environment_section(d: dict[str, Any]) -> list[str]:
    source: dict[str, Any] = (
        d.get("models") or d.get("api") or next(iter(d["pipelines"].values()), {})
    )
    env: dict[str, Any] = source.get("environment", {})
    out = ["## Machine", ""]
    if not env:
        return [*out, "No result file records the machine.", ""]
    pkgs = env.get("packages", {})
    rows = [
        [
            "CPU",
            f"{env.get('cpu')} ({env.get('physical_cores')} cores, {env.get('logical_cpus')} threads)",
        ],
        ["Memory", f"{env.get('memory_gb')} GB"],
        ["Platform", f"{env.get('platform')}, Python {env.get('python')}"],
        [
            "Pinned to",
            f"{env.get('pinned_cpus')} logical CPUs on separate physical cores {env.get('pinned_cpu_ids') or env.get('server_cpu_ids')} to stand in for the spec's 4 vCPU; {env.get('threads')} threads; no GPU",
        ],
        ["Libraries", ", ".join(f"{k} {v}" for k, v in pkgs.items() if v)],
    ]
    b = d.get("browser")
    if b:
        e = b["environment"]
        rows.append(
            [
                "Browser",
                f"Chromium {e['chromium']} (Playwright, headless), viewport {e['viewport']}",
            ]
        )
    return [*out, *table(["", ""], rows)]


def models_section(d: dict[str, Any]) -> list[str]:
    m = d.get("models")
    if not m:
        return []
    out = ["## Model latency (one input at a time, 4 threads)", ""]
    t = m["text_embedding"]
    out += [
        f"**Text embedding** (`{t['model']}`, {t['inputs']}). Load and first call: {ms(t['first_call_ms'], 0)} ms. "
        f"Taxonomy embeddings built once: {ms(t['taxonomy_embedding_build_ms'], 0)} ms. Peak memory {t['peak_rss_mb']} MB.",
        "",
    ]
    out += table(
        ["Measure", "p50 ms", "p95 ms", "max ms", "n"],
        [
            [
                "embed one caption",
                ms(_p(t["embed_one_text_ms"], "p50")),
                ms(_p(t["embed_one_text_ms"])),
                ms(_p(t["embed_one_text_ms"], "max")),
                str(t["embed_one_text_ms"]["n"]),
            ],
            [
                "whole text stage (normalise, embed, score)",
                ms(_p(t["whole_text_stage_ms"], "p50")),
                ms(_p(t["whole_text_stage_ms"])),
                ms(_p(t["whole_text_stage_ms"], "max")),
                str(t["whole_text_stage_ms"]["n"]),
            ],
        ],
    )
    rows = []
    for key, v in m["visual_embedding"].items():
        one, r3, r5 = (
            v["embed_one_frame_ms"],
            v["whole_visual_stage_per_reel_ms"]["3_frames"],
            v["whole_visual_stage_per_reel_ms"]["5_frames"],
        )
        rows.append(
            [
                f"`{key}`",
                ms(_p(one, "p50")),
                ms(_p(one)),
                ms(_p(r3, "p50")),
                ms(_p(r5, "p50")),
                ms(_p(r5)),
                f"{ms(v['first_call_ms'], 0)}",
                str(v["peak_rss_mb"]),
            ]
        )
    out += ["**Visual embedding** (SigLIP, generated 540x960 JPEG frames):", ""]
    out += table(
        [
            "Model",
            "one frame p50",
            "one frame p95",
            "3 frames p50",
            "5 frames p50",
            "5 frames p95",
            "load ms",
            "peak MB",
        ],
        rows,
    )
    rows = []
    for key, v in m["ocr"].items():
        rows.append(
            [
                f"RapidOCR `{key}`",
                ms(_p(v["one_frame_ms"], "p50")),
                ms(_p(v["one_frame_ms"])),
                ms(_p(v["two_frames_per_reel_ms"], "p50")),
                ms(_p(v["two_frames_per_reel_ms"])),
                str(v["mean_text_lines_per_frame"]),
                ms(v["first_call_ms"], 0),
                str(v["peak_rss_mb"]),
            ]
        )
    out += [
        "**OCR** (at most two frames per Reel; generated frames with English overlay text):",
        "",
    ]
    out += table(
        [
            "Engine",
            "one frame p50",
            "one frame p95",
            "2 frames p50",
            "2 frames p95",
            "text lines/frame",
            "load ms",
            "peak MB",
        ],
        rows,
    )
    return out


def contention_section(d: dict[str, Any]) -> list[str]:
    c = d.get("contention")
    if not c:
        return []
    labels = {
        "idle": "text model alone",
        "after_ocr": "right after one OCR call (ONNX Runtime spinning, the default)",
        "after_ocr_with_ort_spinning_off": "right after one OCR call, ONNX Runtime told not to spin",
        "after_ocr_then_250ms_pause": "after one OCR call and a 250 ms pause",
        "after_vision": "right after five SigLIP frames",
        "after_ocr_and_vision": "after OCR and five SigLIP frames",
    }
    rows = []
    for key, label in labels.items():
        v = c["text_embed_ms_by_situation"].get(key)
        if v:
            rows.append([label, ms(_p(v, "p50")), ms(_p(v)), ms(_p(v, "max")), str(v["n"])])
    out = ["## Does one model slow the next? (text embedding time by what ran before it)", ""]
    out += table(["Text embedding of one caption", "p50 ms", "p95 ms", "max ms", "n"], rows)
    return out


def pipeline_section(d: dict[str, Any]) -> list[str]:
    if not d["pipelines"]:
        return []
    out = ["## Worker throughput and the 200-Reel session (N3)", ""]
    rows = []
    for name, p in sorted(d["pipelines"].items()):
        r = p["result"]
        rows.append(
            [
                f"`{name}`",
                f"{p.get('environment', {}).get('pinned_cpus') or 'all'} CPUs, {r['workers']} x {r['threads_per_worker']} thr, ORT {'spins' if r.get('ort_spinning', True) else 'sleeps'}",
                str(r["visual_model"] or "none"),
                f"{r['frames_per_reel']} / {r['ocr_frames_per_reel']} ({r['ocr'] or 'none'})",
                str(r["reels"]),
                ms(r["wall_seconds_for_all_reels"]),
                ms(r["seconds_per_reel_wall"], 2),
                ms(_p(r["per_reel_total_ms"], "p50"), 0),
                ms(_p(r["per_reel_total_ms"]), 0),
                ms(r["fingerprint_rebuild_seconds"] + r["prediction_rebuild_seconds"], 2),
                ", ".join(f"{x:.0f}" for x in r["peak_rss_mb_per_worker"]),
            ]
        )
    out += table(
        [
            "Run",
            "CPUs / workers",
            "Visual",
            "Frames / OCR frames",
            "Reels",
            "Wall s",
            "s per Reel",
            "Reel p50 ms",
            "Reel p95 ms",
            "Rebuild s",
            "Peak MB",
        ],
        rows,
    )
    for name, p in sorted(d["pipelines"].items()):
        r = p["result"]
        out += [f"Where the time goes, `{name}` (median per Reel, one worker's view):", ""]
        stage_rows = [
            [
                "OCR (outside `process_reel`)",
                ms(_p(r["per_reel_ocr_ms"], "p50")),
                ms(_p(r["per_reel_ocr_ms"])),
            ]
        ]
        for stage, v in sorted(r["stage_ms"].items(), key=lambda kv: -(kv[1]["p50"] or 0.0)):
            if (v["p50"] or 0.0) >= 0.5 or (v["p95"] or 0.0) >= 5.0:
                stage_rows.append([stage, ms(v["p50"]), ms(v["p95"])])
        out += table(["Stage", "p50 ms", "p95 ms"], stage_rows)
        if r["failed_stages"]:
            out += [f"**Failed stages:** {', '.join(r['failed_stages'])}", ""]
    return out


def api_section(d: dict[str, Any]) -> list[str]:
    apis = sorted(d.get("apis", {}).items(), key=lambda kv: kv[0] != "api")
    if not apis:
        return []
    out = ["## Dashboard API (N4) and event upload", ""]
    for stem, a in apis:
        main = stem == "api"
        clients = a["dashboard_api"].get("_clients", 5)
        out += [f"### Run `{stem}`", ""]
        s = a.get("seeded_database")
        if s:
            sizes = ", ".join(
                f"{k} {v['rows']:,} rows / {v['total_mb']} MB"
                for k, v in list(s["tables"].items())[:5]
            )
            out += [
                f"Database: {s['users']} user(s), {s['exposures']:,} exposures ({s['sessions_per_user']} sessions each user), "
                f"{s['topics']} topics, per-user topic scores for every Reel. Largest tables: {sizes}. Seeded in {s['insert_seconds']} s.",
                "",
            ]
        rows = []
        for name, v in sorted(a["dashboard_api"].items()):
            if name.startswith("_"):
                continue
            q, c = v["sequential_ms"], v["five_users_at_once_ms"]
            rows.append(
                [
                    name,
                    f"{v['response_bytes']:,}",
                    ms(_p(q, "p50")),
                    ms(_p(q)),
                    ms(_p(q, "max")),
                    ms(_p(c, "p50")),
                    ms(_p(c)),
                    ms(_p(c, "max")),
                    str(v["errors"]),
                ]
            )
        out += table(
            [
                "Endpoint",
                "bytes",
                "1 client p50",
                "1 client p95",
                "1 client max",
                f"{clients} clients p50",
                f"{clients} clients p95",
                f"{clients} clients max",
                "errors",
            ],
            rows,
        )
        out += [
            f"API process memory at the end: {a['dashboard_api'].get('_server_rss_mb')} MB.",
            "",
        ]
        if main:
            out += [
                "**Event upload** (one round trip over HTTP, API and database on the same machine; the extension also waits up to 15 s before sending, by design):",
                "",
            ]
            rows = []
            for name, v in a["event_upload"].items():
                if name.startswith("_"):
                    continue
                rows.append(
                    [
                        name,
                        f"{v.get('body_bytes', v.get('jpeg_bytes', 0)):,}",
                        ms(_p(v["ms"], "p50")),
                        ms(_p(v["ms"])),
                        ms(_p(v["ms"], "max")),
                    ]
                )
            out += table(["Request", "bytes", "p50 ms", "p95 ms", "max ms"], rows)
        if s:
            rb = s["rebuild"]
            first = next(iter(rb.values()))
            out += [
                f"Rebuilding one user's fingerprints from {s['exposures'] // s['users']:,} Reels took {first['fingerprint_rebuild_s']} s "
                f"({first['snapshots']} snapshots) and the predictions {first['prediction_rebuild_s']} s ({first['prediction_rows']:,} rows). "
                "This is the cost of deleting a session and of `rebuild-fingerprints`; it is not on a dashboard path.",
                "",
            ]
    return out


def _comparison(d: dict[str, Any]) -> list[str]:
    runs = d.get("browsers", {})
    base = next((v for k, v in runs.items() if k != "browser"), None)
    after = runs.get("browser")
    if base is None or after is None:
        return []
    out = ["### Before and after the change to `climb`", ""]
    rows: list[list[str]] = []
    for sb, sa in zip(base["scenarios"], after["scenarios"], strict=False):
        cpu = f"{sb['cpu_slowdown']}x"
        for label, getter in (
            ("metadata read p50 ms", lambda s: _p(s["read_reel_metadata_ms"], "p50")),
            ("metadata read p95 ms", lambda s: _p(s["read_reel_metadata_ms"])),
            ("observer callback p50 ms", lambda s: _p(s["intersection_callback_ms"], "p50")),
            ("observer callback p95 ms", lambda s: _p(s["intersection_callback_ms"])),
            ("observer callbacks over 5 ms", lambda s: s["intersection_callbacks_over_5ms"]),
            (
                "detection from 60% p95 ms",
                lambda s: _p(s["detection_smooth_scroll"]["from_60_percent_ms"]),
            ),
            (
                "frames over 33 ms with the tracker",
                lambda s: s["scroll_with_tracker"]["frames_over_33ms"],
            ),
            ("long tasks with the tracker", lambda s: s["scroll_with_tracker"]["long_tasks"]),
        ):
            rows.append([cpu, label, ms(getter(sb), 2), ms(getter(sa), 2)])
    out += table(["CPU", "Measure", "before", "after"], rows)
    profiles = d.get("profiles", {})
    for name, p in sorted(profiles.items()):
        out += [
            f"Profile of `readReel`, `{name}` ({p['rounds']} calls, mean {p['mean_ms']:.2f} ms): "
            + "; ".join(
                f"{t['key'].split(' ')[0]} {t['ms_per_call']:.2f} ms ({t['share'] * 100:.0f}%)"
                for t in p["top"][:4]
            ),
            "",
        ]
    return out


def browser_section(d: dict[str, Any]) -> list[str]:
    b = d.get("browser")
    if not b:
        return []
    out = ["## Browser: detection latency and overhead (N1, N2)", ""]
    p = b["page"]
    out += [
        f"Generated feed of {p['reels']} Reels, each with {p['extra_elements_per_reel']} extra elements around the `<video>` "
        f"(about {b['scenarios'][0]['dom_elements']:,} elements in all), the real content-script code and Chromium's own observers. "
        "`4x` slows the CPU four times with the Chrome DevTools protocol, a stand-in for a weak machine. "
        'This table is the current code; the change measured below is described under "Bottlenecks".',
        "",
    ]
    rows = []
    for s in b["scenarios"]:
        for label, key in (
            ("smooth scroll", "detection_smooth_scroll"),
            ("instant jump", "detection_instant_jump"),
        ):
            r = s[key]
            f60, cmd = r["from_60_percent_ms"], r["from_scroll_command_ms"]
            rows.append(
                [
                    f"{s['cpu_slowdown']}x",
                    label,
                    ms(_p(f60, "p50")),
                    ms(_p(f60)),
                    ms(_p(f60, "max")),
                    ms(_p(cmd, "p50")),
                    ms(_p(cmd)),
                    str(r["missed"]),
                ]
            )
    out += table(
        ["CPU", "Scroll", "from 60% p50", "p95", "max", "from scroll call p50", "p95", "missed"],
        rows,
    )
    rows = []
    for s in b["scenarios"]:
        rows.append(
            [
                f"{s['cpu_slowdown']}x",
                "metadata read (`readReel`)",
                ms(_p(s["read_reel_metadata_ms"], "p50"), 2),
                ms(_p(s["read_reel_metadata_ms"]), 2),
                ms(_p(s["read_reel_metadata_ms"], "max"), 2),
                "-",
            ]
        )
        io = s["intersection_callback_ms"]
        rows.append(
            [
                f"{s['cpu_slowdown']}x",
                "observer callback (two per Reel: the old one ends, the new one begins)",
                ms(_p(io, "p50"), 2),
                ms(_p(io), 2),
                ms(_p(io, "max"), 2),
                f"{s['intersection_callbacks_over_5ms']} / {io['n']}",
            ]
        )
        for k, v in s["mutation_batches"].items():
            m = v["mutation_callback_ms"]
            rows.append(
                [
                    f"{s['cpu_slowdown']}x",
                    f"mutation batch, {k.replace('_', ' ')}",
                    ms(_p(m, "p50"), 2),
                    ms(_p(m), 2),
                    ms(_p(m, "max"), 2),
                    f"{v['over_5ms']} / {m['n']}",
                ]
            )
    out += table(["CPU", "Work", "p50 ms", "p95 ms", "max ms", "over 5 ms"], rows)
    rows = []
    for s in b["scenarios"]:
        for label, key in (
            ("without the tracker", "scroll_without_tracker"),
            ("with the tracker", "scroll_with_tracker"),
        ):
            r = s[key]
            rows.append(
                [
                    f"{s['cpu_slowdown']}x",
                    label,
                    str(r["frames"]),
                    ms(_p(r["frame_ms"], "p50")),
                    ms(_p(r["frame_ms"])),
                    str(r["frames_over_33ms"]),
                    str(r["frames_over_50ms"]),
                    str(r["long_tasks"]),
                    ms(r["longest_task_ms"], 0),
                ]
            )
    out += [
        "Frames while the feed is flicked from Reel to Reel (three trials of the same length each, alternating):",
        "",
    ]
    out += table(
        [
            "CPU",
            "Run",
            "frames",
            "frame p50 ms",
            "frame p95 ms",
            ">33 ms",
            ">50 ms",
            "long tasks",
            "longest ms",
        ],
        rows,
    )
    out += _comparison(d)
    return out


METHOD = """## Method and limits

- **Nothing here was measured on Instagram or on real Reel frames.** No M0 browser probe has been run and there are no real frames (`docs/m0-ml-bench-summary.md`, `docs/ocr-evaluation.md`). The browser numbers use a generated page; the model numbers use generated 540x960 JPEG frames with English overlay text and generated captions. OCR time depends on how much text there is, so it is the least transferable number.
- **The machine is not the spec's.** The spec says 4 vCPU, no GPU. The runs pin the process to four logical CPUs on separate physical cores and give the models four threads, which stands in for 4 vCPU on faster, otherwise idle hardware; a shared virtual server will be slower. The numbers are a reference and a regression guard, not a promise.
- **The worker is composed by the benchmark.** There is no job consumer and no OCR stage in the application. `app.bench pipeline` runs, one Reel after the other, RapidOCR on at most two frames and then the real `process_reel` (text and visual pipelines, fusion, database writes), and rebuilds the fingerprints and predictions once at the end. A real worker adds job claiming and frame file reads/deletes, which are not measured.
- **Model load time is reported apart** and is not in the per-Reel times; a worker pays it once at start.
- **The API runs as its own process** with its database on the same machine; network latency is not included. Rate limits are raised for the run, otherwise they, not the endpoints, would be measured.
- **Statistics:** p50, p95 and max over the stated number of samples after warm-up; the browser and API samples are few enough that p99 is not meaningful and is not used for a verdict.
- **Seeds are fixed** (captions, frames, topic scores, seed data), so a rerun on the same machine differs only by noise; the result files record the machine and library versions."""

REPRODUCE = """## Reproduce

```
# once:  uv sync --group ml --group bench   (in server/)   and   npm install   (in extension/)
# a PostgreSQL database named *_bench that may be wiped, for example:
#   docker run -d --name ref-bench-db -e POSTGRES_PASSWORD=t -e POSTGRES_DB=ref_bench -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
cd server
python -m app.bench models   --out ../docs/performance/models.json   --model-cache <hf cache>
python -m app.bench pipeline --out ../docs/performance/pipeline-siglip2-5frames.json --database-url postgresql+psycopg://postgres:t@127.0.0.1:55432/ref_bench --model-cache <hf cache> --visual siglip2-base --frames-per-reel 5
python -m app.bench api      --out ../docs/performance/api.json      --database-url postgresql+psycopg://postgres:t@127.0.0.1:55432/ref_bench
cd ../extension && npm run bench
cd ../server && python -m app.bench report
```
`docs/performance/commands.txt` lists the exact commands of the runs in this report."""


def render(d: dict[str, Any]) -> str:
    out = [
        "# Performance report",
        "",
        "Generated by `python -m app.bench report` from `docs/performance/*.json`. Targets are the non-functional requirements N1 to N4 of `docs/final-spec.md` §4.",
        "",
        "## Summary against the targets",
        "",
        *table(["Requirement", "Target", "Measured", "Verdict"], summary_rows(d)),
        *environment_section(d),
        METHOD,
        "",
        *models_section(d),
        *contention_section(d),
        *pipeline_section(d),
        *api_section(d),
        *browser_section(d),
    ]
    if d.get("analysis"):
        out += [d["analysis"], ""]
    out += [REPRODUCE, ""]
    return "\n".join(out).rstrip() + "\n"
