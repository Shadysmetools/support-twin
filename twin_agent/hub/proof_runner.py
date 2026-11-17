"""PROVE-IT (stage 9): ShipShape drives the proof-video pipeline that the twin already has (``proof/pipeline.py``).

``RealProofRunner`` delegates to ``proof.pipeline.run`` with the REAL adapters — in observe mode (no staging / no
keys) every step blocks and the artifact is ``blocked`` with the reasons, so nothing is captured, synthesised or
uploaded. ``FakeProofRunner`` returns a deterministic ``prepared`` artifact with an unmistakably-fake Wiki
share link (``wiki.invalid``), used by the hub's CI run. A separate test drives ``proof.pipeline.run`` with
``proof.fakes.fake_adapters()`` directly to prove the reuse path is green end-to-end.
"""
from __future__ import annotations


class FakeProofRunner:
    """Canned ShipShape/proof artifact. ``verdict`` drives the resolve path; the fake link is clearly simulated."""

    def __init__(self, *, verdict: str = "reproduced"):
        self.verdict = verdict
        self.calls: list[dict] = []

    def prove(self, request: dict, packet: dict, cfg: dict) -> dict:
        rid = str(request.get("id") or packet.get("request_id") or "REQ-0000")
        client = str(request.get("company") or packet.get("company") or "UNKNOWN")
        ref = str(request.get("ticket_ref") or rid)
        self.calls.append({"rid": rid, "client": client})
        return {"status": "prepared", "route": "proof_pair", "verdict": self.verdict,
                "share_link": f"https://wiki.invalid/s/{client}/{ref}",
                "video_path": f"data/proof/{rid}/proof.mp4", "simulated": True,
                "note": "ShipShape capture simulated (fake adapters); real run needs staging + keys"}


class RealProofRunner:
    """Drives ``proof.pipeline.run`` with the real adapters. Blocks (never fabricates) without staging/keys."""

    def prove(self, request: dict, packet: dict, cfg: dict) -> dict:
        from ..proof import pipeline
        rid = str(request.get("id") or packet.get("request_id") or "REQ-0000")
        art = pipeline.run(rid, cfg or {}, request=request, packet=packet)
        return {"status": art.get("status"), "route": art.get("route"), "verdict": art.get("verdict"),
                "share_link": art.get("share_link"), "blockers": art.get("blockers"), "why": art.get("why"),
                "simulated": bool(art.get("simulated"))}
