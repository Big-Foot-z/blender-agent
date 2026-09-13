"""One JSON document that answers "did this UV pass, and why" (``quality_report.json``).

Gate **G15**: every automatic UV run must ship a single, self-contained, machine-readable
verdict — the profile it was judged against, the one boolean a reviewer cares about, the
named hard failures, and the per-gate section each failure came from. Nothing here
measures anything: it is a PURE projection of the measurement dict produced by
:func:`chart_uv_agent.refinement_loop.measure_layout` plus two optional blocks the later
stages own (merge-back, shading policy).

Layout of the document::

    schema_version / profile_id / metric_version / calibrated
    passed / hard_failures / quality_failures
    distortion_summary
    layers                       the five review layers + their pass/fail
    sections.distortion          G3  (quality profile vs the v2 distortion report)
    sections.catastrophic        CG2/CG3 (the hard catastrophic distortion gate)
    sections.repair              CG2 (what the catastrophic repair loop bought)
    sections.correctness         G1/G9 (overlap, flips, degenerates, bounds, gaps)
    sections.mandatory           G2  (90° mandatory seam audit)
    sections.fragmentation       G7  (island count / dust / slivers)
    sections.texel_density       G8  (density uniformity)
    sections.packing             G11 (advisory packing efficiency)
    sections.island_connectivity G3  (seam islands vs UV islands)
    sections.border_inset        G9  (the uniform tile-padding repair, if any)
    sections.merge_back          (later stage; ``None`` when not run)
    sections.shading             (later stage; ``None`` when not run)

``hard_failures`` is the measurement's own list, EXTENDED — never replaced — with
``merge_back_incomplete`` / ``shading_policy_failed`` when those optional blocks say so,
so the report's ``passed`` and its failure list always agree.

The whole document goes through :func:`chart_uv_agent.reporting.json_safe`, so it carries
no numpy scalars and no ``NaN``/``Infinity`` tokens: ``json.dumps`` accepts it as-is.
"""

from __future__ import annotations

from chart_uv_agent.reporting import json_safe
from uv_agent.geometry.catastrophic_distortion import compact_catastrophic
from uv_agent.geometry.fragmentation import compact_fragmentation
from uv_agent.geometry.texel_density import compact_texel_density

#: Bumped whenever the shape of the document changes incompatibly.
SCHEMA_VERSION = 1


def _distortion_section(quality: dict) -> dict:
    quality = quality or {}
    return {
        "passed": bool(quality.get("passed", False)),
        "valid": bool(quality.get("valid", False)),
        "failures": list(quality.get("failures") or []),
        "invalid_reasons": list(quality.get("invalid_reasons") or []),
        "checks": list(quality.get("checks") or []),
    }


def _correctness_section(correctness: dict) -> dict:
    correctness = correctness or {}
    return {
        "passed": bool(correctness.get("passed", False)),
        "checks": [
            {
                "name": str(check.get("name")),
                "passed": bool(check.get("passed", False)),
                "value": check.get("value"),
                "limit": check.get("limit"),
            }
            for check in (correctness.get("checks") or ())
        ],
    }


def _mandatory_section(audit: dict) -> dict:
    audit = dict(audit or {})
    audit["passed"] = bool(
        int(audit.get("mandatory_90_missing", 0) or 0) == 0
        and int(audit.get("mandatory_90_uv_unsplit", 0) or 0) == 0
    )
    return audit


def _fragmentation_section(report: dict) -> dict:
    section = compact_fragmentation(report or {})
    section["checks"] = list((report or {}).get("checks") or [])
    return section


def _catastrophic_section(report: dict) -> dict:
    """CG2/CG3: the hard catastrophic verdict as a compact section + its three checks."""
    report = report or {}
    section = compact_catastrophic(report)
    hard_failed = bool(report.get("hard_failed", False))
    region_failed = bool(report.get("region_failed", False))
    valid = bool(report.get("valid", False))
    section["passed"] = bool(report.get("passed", False)) and valid
    section["checks"] = [
        {"name": "catastrophic_hard", "passed": not hard_failed},
        {"name": "catastrophic_region", "passed": not region_failed},
        {"name": "catastrophic_valid", "passed": valid},
    ]
    return section


def _named_checks_pass(section: dict, names) -> bool:
    """True when every named check in ``section['checks']`` passed (a missing check is a
    fail — an unmeasured padding gate may never read as clean)."""
    checks = {str(c.get("name")): bool(c.get("passed", False))
              for c in (section or {}).get("checks") or ()}
    return all(checks.get(name, False) for name in names)


def _layers(sections: dict, *, mandatory_ok: bool) -> list[dict]:
    """The five review layers (A..E), each with the pass computed from the sections."""
    correctness = sections.get("correctness") or {}
    catastrophic = sections.get("catastrophic") or {}
    distortion = sections.get("distortion") or {}
    fragmentation = sections.get("fragmentation") or {}
    texel = sections.get("texel_density") or {}
    merge_back = sections.get("merge_back")
    shading = sections.get("shading")

    frag_hard = fragmentation.get("hard_passed")
    frag_hard_ok = bool(fragmentation.get("passed", False) if frag_hard is None
                        else frag_hard)
    return [
        {"name": "A_correctness",
         "passed": bool(correctness.get("passed", False)) and bool(mandatory_ok)},
        {"name": "B_catastrophic", "passed": bool(catastrophic.get("passed", False))},
        {"name": "C_island_quality", "passed": bool(distortion.get("passed", False))},
        {"name": "D_seam_economy",
         "passed": frag_hard_ok and (merge_back is None
                                     or bool(merge_back.get("complete", True)))},
        {"name": "E_game_production",
         "passed": (bool(texel.get("passed", False))
                    and _named_checks_pass(correctness, ("island_gap", "border_gap"))
                    and (shading is None or bool(shading.get("passed", True))))},
    ]


def build_quality_report(measurement: dict, profile, *,
                         merge_back: dict | None = None,
                         shading: dict | None = None,
                         repair: dict | None = None) -> dict:
    """Project ``measurement`` (+ the optional later-stage blocks) into the G15 document.

    Pure and side-effect free — it never measures, never unwraps and never mutates its
    inputs. ``merge_back`` / ``shading`` are carried through verbatim and are allowed to
    be ``None`` ("that stage did not run"); a block that ran and failed
    (``complete: False`` / ``passed: False``) flips the report's ``passed`` and appends
    its own hard-failure code.
    """
    measurement = measurement or {}
    quality = measurement.get("quality") or {}

    hard_failures = list(measurement.get("hard_failures") or [])
    merge_back_ok = merge_back is None or bool(merge_back.get("complete", True))
    shading_ok = shading is None or bool(shading.get("passed", True))
    if not merge_back_ok:
        hard_failures.append("merge_back_incomplete")
    if not shading_ok:
        hard_failures.append("shading_policy_failed")

    passed = bool(measurement.get("passed", False)) and merge_back_ok and shading_ok

    catastrophic_section = _catastrophic_section(measurement.get("catastrophic") or {})
    if not catastrophic_section["passed"] and "catastrophic_failed" not in hard_failures:
        hard_failures.append("catastrophic_failed")

    report = {
        "schema_version": SCHEMA_VERSION,
        "profile_id": str(getattr(profile, "profile_id", quality.get("profile_id", ""))),
        "metric_version": int(getattr(profile, "metric_version",
                                      quality.get("metric_version", 0))),
        "calibrated": bool(getattr(profile, "calibrated",
                                   quality.get("calibrated", False))),
        "passed": passed,
        "hard_failures": hard_failures,
        "quality_failures": list(measurement.get("quality_failures") or []),
        "distortion_summary": (measurement.get("distortion_v2") or {}).get("summary"),
        "uv_hash": measurement.get("uv_hash"),
        "sections": {
            "distortion": _distortion_section(quality),
            "catastrophic": catastrophic_section,
            "repair": repair,
            "correctness": _correctness_section(measurement.get("correctness") or {}),
            "mandatory": _mandatory_section(measurement.get("mandatory_audit") or {}),
            "fragmentation": _fragmentation_section(measurement.get("fragmentation") or {}),
            "texel_density": compact_texel_density(measurement.get("texel_density") or {}),
            "packing": measurement.get("packing"),
            "island_connectivity": {
                "seam_islands": int(measurement.get("island_count", 0) or 0),
                "uv_islands": int(measurement.get("uv_island_count", 0) or 0),
                "passed": not bool(measurement.get("islands_disagree", False)),
            },
            "border_inset": measurement.get("border_inset"),
            "merge_back": merge_back,
            "shading": shading,
        },
    }
    report["layers"] = _layers(report["sections"],
                               mandatory_ok=bool(report["sections"]["mandatory"]
                                                 .get("passed", False)))
    return json_safe(report)


__all__ = ["SCHEMA_VERSION", "build_quality_report"]
