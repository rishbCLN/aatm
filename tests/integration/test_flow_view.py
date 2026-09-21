"""Tests for the interactive flow view + automatic breaking analysis."""

from __future__ import annotations

from aatm.engine import AATMEngine
from aatm.reporting.flow_view import (FlowVisualizer, build_flow_model,
                                      render_flow_html)


async def _run_report(tmp_config, injection=None):
    engine = AATMEngine(tmp_config)
    output = await engine.run("workflows/travel_booking.yaml",
                              injection_path=injection, backoff_scale=0.0)
    return engine, output, engine.report(output)


async def test_flow_happy_path_reports_no_break(tmp_config):
    _engine, _out, rep = await _run_report(tmp_config)
    model = build_flow_model(rep)
    assert model["analysis"]["broke"] is False
    # Every step should be a successful node in the flow.
    assert all(n["status"] == "success" for n in model["nodes"])
    # The pivot is marked.
    assert any(n["is_pivot"] for n in model["nodes"])


async def test_flow_pre_pivot_break_analysis(tmp_config):
    _engine, _out, rep = await _run_report(
        tmp_config, injection="injections/step3_failure.yaml")
    a = build_flow_model(rep)["analysis"]
    assert a["broke"] is True
    assert a["break_step"] == "step-3"
    assert a["phase"] == "pre-pivot"
    assert "compensation" in a["recovery_mode"]
    assert a["recovery_ok"] is True
    # A resource-unavailable failure yields the domain-outcome guidance.
    assert "resource" in a["root_cause"].lower() or "unavailable" in a["root_cause"].lower()


async def test_flow_pre_pivot_node_states(tmp_config):
    _engine, _out, rep = await _run_report(
        tmp_config, injection="injections/step3_failure.yaml")
    model = build_flow_model(rep)
    states = {n["step_id"]: n["status"] for n in model["nodes"]}
    assert states["step-3"] == "failure"          # the break
    assert states["step-1"] == "compensated"      # rolled back
    assert states["step-2"] == "compensated"
    assert states["step-4"] == "not_reached"      # payment never attempted


async def test_flow_post_pivot_forward_recovery(tmp_config):
    _engine, _out, rep = await _run_report(
        tmp_config, injection="injections/post_pivot_timeout.yaml")
    a = build_flow_model(rep)["analysis"]
    assert a["broke"] is True
    assert a["phase"] == "post-pivot"
    assert "forward recovery" in a["recovery_mode"]
    # The irreversibility caveat must be surfaced as residual risk.
    assert any("irreversible" in r.lower() or "refund" in r.lower()
               for r in a["residual_risks"])


async def test_flow_html_renders_and_writes(tmp_config):
    _engine, out, rep = await _run_report(
        tmp_config, injection="injections/step3_failure.yaml")
    html = render_flow_html(rep)
    assert "Flow &amp; Breaking Analysis" in html   # page title/heading
    assert "Broke at step-3" in html                # computed break headline
    assert "step-3" in html
    # Writes a self-contained file (no external <script src>).
    path = FlowVisualizer(tmp_config).from_report_dict(rep, str(out.run.run_id))
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "<script>" in content
    assert "src=\"http" not in content  # no external JS dependency


async def test_flow_html_embeds_replay_scrubber(tmp_config):
    """The flow view ships a deterministic-replay timeline for time-travel."""
    _engine, out, rep = await _run_report(
        tmp_config, injection="injections/step3_failure.yaml")
    path = FlowVisualizer(tmp_config).from_report_dict(rep, str(out.run.run_id))
    content = path.read_text(encoding="utf-8")
    # The scrubber UI + the embedded, non-empty replay frame array are present.
    assert 'id="scrub"' in content
    assert "const REPLAY = [" in content
    assert "initScrubber()" in content
    # Frames reconstruct real events from this run (e.g. the compensation).
    assert "COMPENSATION" in content
