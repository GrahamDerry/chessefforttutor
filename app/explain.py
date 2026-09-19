"""Templated explanations for the reveal panel (SPEC.md §5 B4).

Pure functions of the scenario row so they are unit-testable. An LLM narrator can
later replace `explain()` behind the same interface.
"""
from typing import Mapping

CRIT = 0.10
CALM = 0.03


def _secs(v: float | None) -> str:
    if v is None:
        return "an unknown amount of time"
    return f"{v:.0f} s" if v >= 10 else f"{v:.1f} s"


def _pts(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}"


def why_label(r: Mapping) -> str:
    """Why this position is LONG or SHORT, from the position alone."""
    c, obvious, commitment = r["criticality"], r["obvious"], r["commitment"]
    if r["ground_truth"] == "LONG":
        if commitment is not None and commitment > 0:
            return (f"The engine rates several moves as equal, but they lead to very "
                    f"different games (commitment {_pts(commitment)}). A fork in the road "
                    f"deserves time even when nothing is hanging.")
        if obvious:
            # Should not occur under the spec's label rule, but never show a
            # contradiction if the pipeline hands us one.
            return (f"The position is sharp (criticality {_pts(c)}) — one move holds it and "
                    f"the rest give ground. Worth confirming before you commit.")
        return (f"Only one move really held (criticality {_pts(c)}), and it is not the move "
                f"fast intuition finds first. Positions like this punish a quick decision.")
    if obvious:
        return ("Fast intuition already finds the right move here — a shallow search picks "
                "the same move as a deep one. Play it and keep your clock.")
    return (f"Several moves are fine (criticality {_pts(c)}). There is nothing to calculate; "
            f"spending time here buys you nothing.")


def what_happened(r: Mapping) -> str:
    """What the user actually did in the real game."""
    spent, loss = r["seconds_spent"], r["e_loss"]
    frac = r["time_fraction"]
    pieces = [f"You played {r['move_san']} in {_secs(spent)}"]
    if frac is not None:
        pieces.append(f"({frac * 100:.0f}% of your remaining clock)")
    tail = ". "
    if loss is not None and loss >= 0.20:
        tail = f", losing {_pts(loss)} expected points. "
    elif loss is not None and loss >= 0.05:
        tail = f", costing {_pts(loss)} expected points. "
    return " ".join(pieces) + tail


KIND_NOTE = {
    "blunder": "This was a blunder in the game.",
    "too_little": "You moved faster than your own typical pace here — and it cost you.",
    "too_much": "You burned real clock on a position that did not need it.",
    "lost_advantage": "This is the move where a winning position started slipping away.",
    "fork": "Equal evaluations, different games — this is a commitment decision.",
    "calm": "Nothing was going on here. Banking time in positions like this is what "
            "pays for the ones that matter.",
}


def explain(r: Mapping) -> str:
    return " ".join(filter(None, [
        why_label(r),
        what_happened(r),
        KIND_NOTE.get(r["kind"], ""),
    ])).strip()
