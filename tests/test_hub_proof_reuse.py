"""PROVE-IT reuse: the hub's ShipShape stage drives the twin's existing ``proof/pipeline.py``. This proves the
reuse path is green with the proof pipeline's own fake adapters, and that the REAL proof runner blocks (never
fabricates) in observe mode with no staging/keys. Uses tmp_home via the ``cfg`` fixture so ``proof.key`` and the
proof working dir land under a throwaway home, not the real one."""
from __future__ import annotations

from twin_agent.proof import pipeline
from twin_agent.proof.fakes import fake_adapters
from twin_agent.testing.fakes import canned_packet
from twin_agent.hub.proof_runner import FakeProofRunner, RealProofRunner


def test_proof_pipeline_green_with_fake_adapters(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"), adapters=fake_adapters())
    assert out["status"] == "prepared" and out["route"] == "proof_pair" and out["simulated"] is True


def test_real_proof_runner_blocks_in_observe(cfg):
    out = RealProofRunner().prove({"id": "REQ-1", "company": "EG"},
                                  {"request_id": "REQ-1", "tier": "T2b"}, cfg)
    assert out["status"] in ("blocked", "needs_human") and out["share_link"] is None


def test_fake_proof_runner_returns_prepared_artifact():
    out = FakeProofRunner().prove({"id": "REQ-9", "company": "EG", "ticket_ref": "123"}, {}, {})
    assert out["status"] == "prepared" and out["share_link"].startswith("https://wiki.invalid/")
