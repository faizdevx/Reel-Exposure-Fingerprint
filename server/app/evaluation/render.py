"""The evaluation report as Markdown, written from the numbers and from nothing else.

Every sentence here is chosen by the figures: there is no sentence that reads as success unless a
figure says so, and a missing figure is printed as missing. Edit the code, never the output.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.prediction.config import MODELS

MODEL_NAMES = {
    "uniform": "Uniform",
    "most_frequent": "Most frequent topic",
    "decayed_fingerprint": "Decayed fingerprint",
    "markov": "Markov transition",
    "hybrid": "Hybrid",
}
MODES = (("text_only", "Text only"), ("visual_only", "Visual only"), ("fused", "Fused"))


def pct(value: float | None) -> str:
    return "n/a" if value is None or value != value else f"{value * 100:.1f}%"


def num(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None or value != value else f"{value:.{digits}f}"


def interval(entry: Mapping[str, Any] | None) -> str:
    if not entry or entry.get("estimate") is None:
        return "n/a"
    low, high = entry.get("low"), entry.get("high")
    if low is None or high is None:
        return pct(entry["estimate"])
    return f"{pct(entry['estimate'])} ({pct(low)} to {pct(high)})"


def table(header: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(row) + " |" for row in rows]
    return [*out, ""]


def short(user: str) -> str:
    return user[:8]


# ------------------------------------------------------------------ 1


def labelling(section: Mapping[str, Any]) -> list[str]:
    out = ["## 1. Topic labelling", ""]
    bars = section["pass_bars"]
    out.append(
        f"Spec §10 bar: fused top-1 at least {bars['top1']:.0%} and top-3 at least "
        f"{bars['top3']:.0%} on 200 hand-labelled Reels."
    )
    out.append("")
    if section["status"] == "not_run":
        out += [f"**Not run.** {section['reason']}", ""]
        out += [
            "No figure is given. The harness is ready: `python -m app.evaluation embed`, then "
            "`evaluate`, then `report --labelling-report`.",
            "",
        ]
        return out
    figures = section["figures"]
    out.append(
        f"Labelled Reels: {section['labelled_reels']}. Scored together (Reels with text and "
        f"frames): {section['paired_reels']}. The three modes are scored on the same Reels, at the "
        "pipeline defaults, with 95% cluster-bootstrap intervals over sources."
    )
    out.append("")
    if section["reason"]:
        out += [f"**Below the minimum.** {section['reason']}", ""]
    if section["paired_reels"] == 0:
        out += [
            "**No figure.** No labelled Reel has both text and frames, so none of the three modes "
            "can be scored on the same Reels.",
            "",
        ]
        return out
    rows = [
        [label, interval(figures[key]["top1"]), interval(figures[key]["top3"])]
        for key, label in MODES
    ]
    out += table(["Mode", "Top-1", "Top-3"], rows)
    if section["passed"] is None:
        out.append("The pass bar cannot be judged on this set.")
    elif section["passed"]:
        out.append("Fused figures meet the bar (point estimates).")
    else:
        out.append(
            "**Fused figures do not meet the bar** (point estimates). The taxonomy must be fixed "
            "before anything downstream is trusted."
        )
    out.append("")
    return out


# ------------------------------------------------------------------ 2


def self_report(section: Mapping[str, Any]) -> list[str]:
    out = ["## 2. Fingerprint against self-report", ""]
    out.append(
        "Each user rated their interest in every topic from 1 to 5, before seeing a fingerprint. "
        "Spearman's rho is computed per user between the ratings and each fingerprint's topic "
        "shares (a topic the fingerprint never saw has share 0). `at survey` uses only Reels "
        "observed before the survey was completed; `all data` uses every Reel."
    )
    out.append("")
    if section["status"] == "not_run":
        out += [
            f"**Not run.** {section['users_with_ratings']} of {section['users_total']} users have "
            "ratings. No correlation can be stated.",
            "",
        ]
        return out
    out.append(
        f"Users with ratings: {section['users_with_ratings']} of {section['users_total']}. "
        f"Minimum for a fingerprint claim: {section['min_reels_per_user']} Reels per user."
    )
    out.append("")
    rows: list[list[str]] = []
    for row in section["per_user"]:
        if row["status"] != "rated":
            rows.append([short(row["user"]), "no ratings", "", "", ""])
            continue
        for view in ("at_survey", "all_data"):
            if view not in row:
                continue
            v = row[view]
            rows.append(
                [
                    short(row["user"]),
                    view.replace("_", " "),
                    str(v["reels"]) + (" (below 500)" if v["below_minimum"] else ""),
                    num(v["exposure"]),
                    num(v["engagement"]),
                ]
            )
    out += table(["User", "Reels up to", "Reels", "Exposure rho", "Engagement rho"], rows)
    for view, name in (("at_survey", "at survey"), ("all_data", "all data")):
        s = section[view]
        d = s["engagement_minus_exposure"]
        out.append(
            f"**{name}** ({s['users']} users): mean exposure rho {num(s['exposure']['mean'])}, "
            f"mean engagement rho {num(s['engagement']['mean'])}; engagement minus exposure "
            f"{num(d['mean'])} (engagement higher for {d['users_engagement_higher']}, exposure "
            f"higher for {d['users_exposure_higher']}, equal for {d['users_equal']}). "
            f"{s['users_below_500_reels']} of these users are below "
            f"{section['min_reels_per_user']} Reels."
        )
        out.append("")
    out.append(
        "The expectation was engagement above exposure; the figures above say whether it held. "
        "With this few users, no interval or significance is given, and none should be inferred."
    )
    out.append("")
    return out


# ------------------------------------------------------------------ 3


def prediction(section: Mapping[str, Any]) -> list[str]:
    out = ["## 3. Prediction (Predicted next observed topic)", ""]
    out.append(
        "Prequential: each prediction was made before its Reel was counted. This predicts which "
        "topic the feed shows next; it says nothing about what a user prefers."
    )
    out.append("")
    if section["status"] == "no_data":
        out += ["**Not run.** No Reel has been scored.", ""]
        return out
    required = section["required_reels_per_user"]
    out.append(
        f"Scored Reels: {section['scored_reels']} across {section['users']} users. Users with at "
        f"least {required} scored Reels: {section['users_complete']}. A comparison is stated only "
        "for such users."
    )
    out.append("")
    out.append("Pooled over all scored Reels (descriptive: users with more Reels weigh more):")
    out.append("")
    rows = [
        [MODEL_NAMES[m], pct(p["top1"]), pct(p["top3"]), num(p["log_loss"])]
        for m in MODELS
        for p in [section["pooled"][m]]
    ]
    out += table(["Model", "Top-1", "Top-3", "Log loss"], rows)
    for user, summary in section["per_user"].items():
        out.append(f"### User {short(user)}: {summary['status']}")
        out.append("")
        window = summary["rolling_window"]
        rows = [
            [
                MODEL_NAMES[m],
                pct(summary["models"][m]["all_time"]["top1"]),
                pct(summary["models"][m]["all_time"]["top3"]),
                num(summary["models"][m]["all_time"]["log_loss"]),
                pct(summary["models"][m]["rolling"]["top1"]),
            ]
            for m in MODELS
        ]
        out += table(["Model", "Top-1", "Top-3", "Log loss", f"Top-1, last {window}"], rows)
        if summary["comparisons"] is None:
            out.append("No comparison and no verdict: the minimum was not reached.")
            out.append("")
            continue
        for name, c in summary["comparisons"].items():
            d = c["log_loss_difference"]
            out.append(
                f"- Hybrid minus {MODEL_NAMES[name]}, log loss per Reel: {num(d['estimate'])} "
                f"(95% interval {num(d['low'])} to {num(d['high'])}): `{c['log_loss_verdict']}`"
            )
        out.append("")
        if summary["hybrid_beats_required_baselines"]:
            out.append("The hybrid's log loss was clearly lower than both required baselines.")
        else:
            out.append(
                "**The hybrid did not clearly beat both required baselines** (most frequent "
                "topic, decayed fingerprint)."
            )
        out.append("")
    return out


# ------------------------------------------------------------------ 4


def stability(section: Mapping[str, Any]) -> list[str]:
    out = ["## 4. Fingerprint stability", ""]
    out.append(
        f"Pass bar (spec §10): {section['pass_bar']}. Baseline: {section['baseline']}. Each "
        f"variant replays the same Reels; shuffling permutes whole sessions "
        f"({section['shuffles']} random orders, seeded), keeping the Reels of a session in order "
        "and the user's own gaps between sessions."
    )
    out.append("")
    if section["status"] == "no_data":
        out += ["**Not run.** No user has Reels.", ""]
        return out
    for user, s in section["per_user"].items():
        note = (
            f" (below the {section['min_reels_per_user']} Reel minimum: descriptive only)"
            if s["below_minimum"]
            else ""
        )
        out.append(f"### User {short(user)}: {s['reels']} Reels{note}")
        out.append("")
        for kind in ("exposure", "engagement"):
            out.append(f"**{kind}**, baseline top 5: {s['baseline_top5'][kind]}")
            out.append("")
            rows: list[list[str]] = []
            for half_life, per_kind in s["half_life"].items():
                c = per_kind[kind]
                rows.append(
                    [
                        f"half-life {half_life} days",
                        str(c["top5"]),
                        "yes" if c["same_top5_order"] else "**no, order changed**",
                        "yes" if c["same_top5_set"] else "no",
                        num(c["kendall_tau_all_topics"]),
                    ]
                )
            sh = s["shuffled_session_order"][kind]
            if sh["shuffles"]:
                rows.append(
                    [
                        f"shuffled session order ({sh['sessions']} sessions)",
                        "varies",
                        f"{sh['same_top5_order']} of {sh['shuffles']} orders kept it",
                        f"{sh['same_top5_set']} of {sh['shuffles']}",
                        f"mean {num(sh['kendall_tau_mean'])}, min {num(sh['kendall_tau_min'])}",
                    ]
                )
            else:
                rows.append(["shuffled session order", "n/a", "needs two sessions", "n/a", "n/a"])
            out += table(
                [
                    "Variant",
                    "Top 5",
                    "Same order as baseline",
                    "Same set",
                    "Kendall tau, all topics",
                ],
                rows,
            )
    out.append(
        "Half-life 3 and 14 days are not small changes to half-life 7; a flip there is reported, "
        "not excused."
    )
    out.append("")
    return out


# ------------------------------------------------------------------ whole


def render(report: Mapping[str, Any]) -> str:
    meta = report["meta"]
    out = ["# Evaluation report", ""]
    out.append(
        "Generated by `python -m app.evaluation report`. Every figure comes from stored data and "
        "the code in `server/app/evaluation/research.py`; none was adjusted. A result that falls "
        "short is reported as it fell. Where the data is below the minimum of spec §10, the "
        "figure is a description and no claim follows from it."
    )
    out.append("")
    note = report.get("note")
    if note:
        out += [f"> **{note}**", ""]
    out += table(
        ["Input", "Value"],
        [[k, f"`{v}`"] for k, v in meta.items()] + [["seed", f"`{report['seed']}`"]],
    )
    out += labelling(report["labelling"])
    out += self_report(report["self_report"])
    out += prediction(report["prediction"])
    out += stability(report["stability"])
    return "\n".join(out).rstrip() + "\n"
