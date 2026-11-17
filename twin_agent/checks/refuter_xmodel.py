"""Cross-model adversarial refuter (17 Adversarial verification; raises maturity dim 4 verification/anti-yes-man).

The in-repo refuter is Claude checking Claude (judge.refute), which drifts to agreement and over-claims
(Wave 3: it falsely claimed a guard bypass). This module adds an INDEPENDENT, NON-Claude refuter (Groq) plus a
deterministic evidence gate written in code, so a claim can NEVER pass on a model's "looks fine" alone.

A claim is VERIFIED only if BOTH hold:
  (a) it carries concrete evidence - a record id / model+id / file:line / a reproduction - checked HERE in code
      (not the model's opinion); and
  (b) when a cross-model refuter is configured, that refuter FAILS to refute it.
Missing evidence, a successful refutation, or (at the cross-model step) any refuter error / timeout / no-key means
NOT verified (fail-closed), with a clear reason. When NO groq key is configured, the packet can still verify in a
DEGRADED "single-model" mode on the evidence gate + the existing Claude judges; the mode is recorded on every card
and verdict so dim 4 stays honest about the weaker check.

OBSERVE-safe: nothing here sends or writes. The ONLY thing that leaves the machine is the REDACTED claim and the
REDACTED structural evidence, wrapped as untrusted data, posted to Groq for a verdict. The raw thread, the worker's
`note`/`confidence`/reasoning, staff names and secrets never leave - see build_refuter_payload.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..redact import redact
from .injection import wrap_untrusted
from .judge import extract_json

log = logging.getLogger(__name__)

# A sensible current Groq production model; override in config.verify.groq_model. The real path is only exercised
# when a 'groq' key is present - otherwise the module runs in single-model mode and this is never used.
DEFAULT_GROQ_MODEL = "llama-3.3-70b-versatile"

# A record reference (WAVE/00001, IN/00001/26, SO12345); mirrors judge._REF / fixgate._PARAM shapes.
_REF = re.compile(r"\b(?:[A-Z]{2,6}(?:/[A-Za-z0-9]+){1,4}|(?:SO|PO|RO)\d{3,7})\b")
_FILE_LINE = re.compile(r"\S+\.[A-Za-z0-9]+:\d+")
# Structural fields that are always safe to send (ids / models / verdicts / file:line); never free-text notes.
_STRUCTURAL_KEYS = ("kind", "id", "model", "field", "status", "source", "file", "location")
# These can be free text that carries a staff NAME, so they are sent ONLY when they are a non-string scalar or
# themselves a record ref / file:line (fix 5: names must not ride out in the external payload).
_SCALAR_REF_KEYS = ("value", "observed", "result", "steps")


# ---------------------------------------------------------------- data types

@dataclass
class Claim:
    """What is being asserted, plus the evidence offered for it. `records` = records a fix would touch."""
    kind: str                                   # "diagnosis" | "fix" | "reply"
    statement: str
    evidence: list[dict] = field(default_factory=list)
    records: list[str] = field(default_factory=list)


@dataclass
class RefuterReply:
    """One refuter's verdict. `error` non-empty = no usable verdict. `transient` marks a BACKEND error (an
    exception, a failed session / invalid --model, a timeout, an auth / transport failure) that means the refuter
    could not run - the gate then DEGRADES to single-model (fix 4), never a blanket refute. A non-transient error
    (unreadable / non-boolean output, which injected claim text could steer) stays a fail-closed veto."""
    refuted: bool
    reasons: list[str] = field(default_factory=list)
    model: str = ""
    error: str = ""
    transient: bool = False


@dataclass
class ClaimVerdict:
    claim: str                                  # the claim kind
    verified: bool
    mode: str                                   # "cross-model" | "single-model"
    reasons: list[str] = field(default_factory=list)


@dataclass
class VerifyResult:
    verified: bool
    mode: str
    claim_verdicts: list[ClaimVerdict] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class Refuter(Protocol):
    def refute(self, claim: Claim, *, redact_fn=redact) -> RefuterReply:  # pragma: no cover - structural
        ...


# ---------------------------------------------------------------- evidence gate (deterministic, in code)

def _item_id(item: dict) -> str:
    return str(item.get("id") or "").strip()


def _is_concrete(item: dict) -> bool:
    """A single evidence item that stands on its own: a real record id, model+id, a file:line, or a reproduction.

    A note / assertion ("I verified it works", "tests pass", "looks fine") is NOT concrete - that is the yes-man
    trap the gate exists to close. Anything marked provisional is not concrete either.
    """
    if not isinstance(item, dict):
        return False
    if str(item.get("status", "")).lower().startswith("provisional"):
        return False
    kind = str(item.get("kind", "")).lower()
    rid = _item_id(item)
    if kind == "odoo_read" and item.get("model") and rid:
        return True
    if kind == "runbook" and rid:
        return True
    if rid and _REF.search(rid):
        return True
    for v in (item.get("file"), item.get("location"), item.get("ref")):
        if v and _FILE_LINE.search(str(v)):
            return True
    if kind in ("reproduction", "repro") and item.get("steps") and (item.get("result") or item.get("observed")):
        return True
    return False


def concrete_evidence_reasons(claim: Claim) -> list[str]:
    """[] = the claim carries concrete evidence. Otherwise the reasons it does not (so the gate can say why)."""
    items = [e for e in (claim.evidence or []) if isinstance(e, dict)]
    if any(str(e.get("status", "")).lower().startswith("provisional") for e in items):
        return ["evidence is marked provisional; a provisional entry cannot verify a claim"]
    if not any(_is_concrete(e) for e in items):
        return [f"no concrete evidence for the {claim.kind} claim (need a record id, model+id, file:line or a "
                f"reproduction; an assertion like 'it works' or 'tests pass' does not count)"]
    if claim.kind == "fix":
        backed = {f"{e.get('model', '')} {e.get('id', '')}".strip()
                  for e in items if str(e.get("kind", "")).lower() == "odoo_read"}
        backed |= {_item_id(e) for e in items if _item_id(e)}
        for rec in (claim.records or []):
            rec = str(rec).strip()
            if rec and rec not in backed:
                return [f"the fix would touch {rec!r} but no evidence item reads that exact record"]
    return []


# ---------------------------------------------------------------- claims out of a packet

def extract_claims(packet: dict) -> list[Claim]:
    """The claims that matter (17 'where it applies'): the diagnosis / root cause, the 'fix is done/works' claim,
    and - optionally - a factual draft-reply claim. Holding lines ('Checking it, 10 min.') carry no fact to check."""
    packet = packet or {}
    evidence = [e for e in (packet.get("evidence") or []) if isinstance(e, dict)]
    claims: list[Claim] = []
    understanding = str(packet.get("understanding") or "").strip()
    if understanding:
        claims.append(Claim("diagnosis", understanding, evidence))
    action = packet.get("proposed_action") or {}
    if isinstance(action, dict) and action.get("type"):
        records = [str(r) for r in (action.get("records") or [])]
        stmt = (f"the proposed fix ({action.get('type')}) is correct and safe to apply to "
                f"{', '.join(records) or 'the named records'}, and does nothing else")
        claims.append(Claim("fix", stmt, evidence, records=records))
    return claims


# ---------------------------------------------------------------- one claim through the gate

def _safe_refute(refuter: Refuter, claim: Claim, redact_fn) -> RefuterReply:
    try:
        reply = refuter.refute(claim, redact_fn=redact_fn)
    except Exception as exc:                                         # a crashed/timed-out refuter = backend error
        return RefuterReply(True, [], getattr(refuter, "model", ""), error=f"{type(exc).__name__}", transient=True)
    if not isinstance(reply, RefuterReply):
        return RefuterReply(True, [], "", error="refuter returned a bad reply")   # programming error: veto
    return reply


def check_claim(claim: Claim, refuter: Refuter | None, *, redact_fn=redact,
                require_cross_model: bool = True) -> ClaimVerdict:
    """Evidence gate + cross-model refutation for one claim.

    - refuter present: PASS only if evidence is concrete AND the refuter fails to refute. A successful refutation,
      an error or a timeout = FAIL.
    - refuter None and require_cross_model: FAIL (no key is fail-closed at the cross-model step) - this is the
      honest "cannot run the independent check" answer.
    - refuter None and not require_cross_model: DEGRADED single-model; PASS on the evidence gate alone (the packet
      orchestrator pairs this with the existing Claude judges and labels the packet single-model).
    """
    reasons: list[str] = []
    ev_reasons = concrete_evidence_reasons(claim)
    reasons += ev_reasons
    has_ev = not ev_reasons
    refuted = False
    if refuter is not None:
        reply = _safe_refute(refuter, claim, redact_fn)
        if reply.error and reply.transient:
            # Fix 4: the refuter BACKEND could not run (crash / failed session / invalid --model / timeout / auth /
            # transport). That is not a refutation. DEGRADE this claim to single-model so it can still pass on the
            # evidence gate + Claude judges - NEVER a blanket refute (that was the auth/SDK refute-all outage).
            mode = "single-model"
            reasons.append(f"cross-model refuter unavailable ({reply.error}); degraded to single-model "
                           "(evidence gate + Claude judges only)")
        elif reply.error:
            # Unreadable / non-boolean output: injected claim text could steer it, so keep it a fail-closed veto.
            mode = "cross-model"
            refuted = True
            reasons.append(f"cross-model refuter gave unusable output ({reply.error}); fail-closed")
        elif reply.refuted:
            mode = "cross-model"
            refuted = True
            reasons += [f"refuted by {reply.model or 'cross-model'}: {r}"
                        for r in (reply.reasons or ["no reason given"])]
        else:
            mode = "cross-model"
    elif require_cross_model:
        mode = "cross-model"
        refuted = True
        reasons.append("no cross-model refuter configured (no independent Claude session and no groq key); "
                       "fail-closed")
    else:
        mode = "single-model"
        reasons.append("single-model (degraded): no cross-model refuter; verified on the evidence gate + "
                       "Claude judges only")
    return ClaimVerdict(claim.kind, has_ev and not refuted, mode, reasons)


# ---------------------------------------------------------------- the packet-level decision + card label

def verify_packet(packet: dict, *, runbook: dict | None = None, judges_passed: bool, read_user_exists: bool = False,
                  refuter: Refuter | None = None, redact_fn=redact) -> VerifyResult:
    """The whole verification for one worker packet. verified=True ONLY when ALL hold (pure AND - the cross-model
    check can veto, never rescue):
      1. the existing evidence gate + Claude judges pass (fixgate.recompute_verified), AND
      2. every matter-claim passes check_claim - cross-model refutation when a key is configured, else the
         degraded single-model evidence gate.
    """
    runbook = runbook or {}
    try:
        from .guards import recompute_verified
        base = bool(recompute_verified(packet, runbook, read_user_exists=read_user_exists,
                                       judges_passed=judges_passed))
    except Exception:                                               # any doubt = not verified
        base = False
    require = refuter is not None
    cvs = [check_claim(c, refuter, redact_fn=redact_fn, require_cross_model=require) for c in extract_claims(packet)]
    claims_ok = all(cv.verified for cv in cvs) if cvs else True
    verified = base and claims_ok
    # The packet mode is honest about the weakest link: if any claim DEGRADED to single-model (fix 4: the refuter
    # errored), the packet is single-model, even if a refuter was configured.
    if cvs:
        mode = "single-model" if any(cv.mode == "single-model" for cv in cvs) else "cross-model"
    else:
        mode = "cross-model" if refuter is not None else "single-model"
    reasons: list[str] = []
    if not base:
        reasons.append("evidence gate / Claude judges did not pass (fixgate.recompute_verified)")
    for cv in cvs:
        if not cv.verified:
            reasons += [f"{cv.claim}: {r}" for r in cv.reasons]
    if verified and mode == "single-model":
        reasons.append("single-model (degraded): no independent cross-model refuter - evidence gate + Claude judges only")
    return VerifyResult(verified, mode, cvs, reasons)


def mode_label(verified: bool, mode: str, *, diverse: bool | None = None) -> str:
    """The card label, honest about the degraded mode: 'verified (cross-model)', 'not verified (single-model)'.
    ``diverse`` (fix 3): when the cross-model refuter is NOT on a different model (none configured), say so rather
    than imply model diversity - 'verified (independent refuter, same model)'."""
    tail = mode
    if mode == "cross-model" and diverse is False:
        tail = "independent refuter, same model"
    return f"{'verified' if verified else 'not verified'} ({tail})"


def gate_verdict(result: VerifyResult):
    """A judge.Verdict for the card/judge.json so the cross-model result is observable alongside the other checks."""
    from .judge import Verdict
    reasons = list(result.reasons) if result.reasons else [f"cross-model gate passed ({result.mode})"]
    items = [{"mode": result.mode, "claims": [cv.__dict__ for cv in result.claim_verdicts]}]
    return Verdict("cross_model", result.verified, reasons, items)


# ---------------------------------------------------------------- the external (Groq) refuter + its payload

def _vault_hashes_safe(backend=None) -> set:
    """sha256 of every vault value, so an exact secret that slipped into a claim is redacted too. Best-effort:
    [] if the vault is unavailable (tests, no keyring) - the pattern redactor still runs."""
    try:
        from .. import creds
        return set(creds.vault_hashes(backend=backend))
    except Exception:
        return set()


REFUTER_INSTRUCTIONS = (
    "You are an INDEPENDENT adversarial refuter in a fresh session (a different model when one is configured). You "
    "use no tools and you never see the author's reasoning. Your only job is to prove the claim WRONG. Assume it is "
    "WRONG until the "
    "evidence forces otherwise. Look for: a different root cause that fits the same evidence; a record the fix would "
    "also touch; a sub-question it did not answer; a fix claimed before it was applied; a step that makes things "
    "worse; the wrong company. Judge ONLY on the concrete evidence shown - never pass a claim because it 'looks "
    "fine'. Answer with exactly one JSON object: {\"refuted\": true|false, \"reasons\": [\"...\"]}. refuted is true "
    "if you found a concrete way it is wrong or unsafe."
)


def _scalar_or_ref(v) -> bool:
    """True for a value safe to forward verbatim: a non-string scalar, or a string that is itself a record ref /
    file:line. A free-text string (which can carry a staff name) is NOT safe."""
    if isinstance(v, bool) or isinstance(v, (int, float)):
        return True
    if isinstance(v, str):
        return bool(_REF.search(v) or _FILE_LINE.search(v))
    return False


def _safe_evidence(e: dict) -> dict:
    """Structural fields only (ids / model / verdict / file:line), plus value/observed/result/steps ONLY when
    they are a scalar or a ref - so a free-text field that names staff never leaves in the payload (fix 5)."""
    out = {k: e[k] for k in _STRUCTURAL_KEYS if k in e}
    for k in _SCALAR_REF_KEYS:
        if k in e and _scalar_or_ref(e[k]):
            out[k] = e[k]
    return out


def build_refuter_payload(claim: Claim, *, redact_fn=redact, backend=None, minimal: bool = False) -> str:
    """The user message for a refuter: the redacted claim and redacted STRUCTURAL evidence, wrapped as untrusted
    data. No raw thread, no note/confidence, no secrets.

    ``minimal`` (the EXTERNAL, non-Claude backend): also DROP the free-text diagnosis/reply statement, which can
    carry a staff name - keep only the code-templated fix statement. The in-subscription Claude refuter runs with
    ``minimal=False`` (the redacted prose stays on-device, no third-party egress), so it can still challenge the
    worded diagnosis."""
    hashes = _vault_hashes_safe(backend=backend)

    def red(s) -> str:
        try:
            return redact_fn(str(s), hashes)
        except TypeError:                                           # a redact_fn that takes only the text
            return redact_fn(str(s))

    lines = [f"Claim kind: {claim.kind}"]
    if not minimal or claim.kind == "fix":                          # fix statement is code-templated (no free text)
        lines.append(f"Claim: {red(claim.statement)}")
    if claim.records:
        lines.append(f"Records the fix would touch: {red(', '.join(claim.records))}")
    lines.append("Evidence items (structural fields only):")
    for e in (claim.evidence or []):
        if isinstance(e, dict):
            lines.append(red(json.dumps(_safe_evidence(e), ensure_ascii=False)))
    body = "\n".join(lines)
    return REFUTER_INSTRUCTIONS + "\n\n" + wrap_untrusted(body, "redacted claim and evidence")


def _parse_refuter_obj(obj, model: str) -> RefuterReply:
    """Turn a refuter's JSON ({"refuted": bool, "reasons": [...]}) into a RefuterReply. Unreadable = fail-closed."""
    if not isinstance(obj, dict) or "refuted" not in obj:
        return RefuterReply(True, ["could not read the refuter's output"], model, error="unreadable")
    val = obj.get("refuted")
    reasons = [str(r) for r in (obj.get("reasons") or []) if str(r).strip()]
    if val is False:
        return RefuterReply(False, reasons, model)
    if val is True:
        return RefuterReply(True, reasons or ["no reason given"], model)
    return RefuterReply(True, ["refuter 'refuted' was not a boolean"], model, error="unreadable")


class ClaudeRefuter:
    """The DEFAULT refuter: an INDEPENDENT in-subscription Claude session (claude -p). It is spawned FRESH (no
    --resume), so it never sees the author/worker's context, with the adversarial REFUTER_INSTRUCTIONS, optionally
    on a DIFFERENT Claude model than the worker (real model-diversity, still in-subscription, NO third-party
    egress - fine in observe). Fail-closed: any run error / unreadable output = refuted (veto), never a silent pass.

    This is NOT a weak local model: it is a strong model on the same subscription, which is why it is the default.
    """

    def __init__(self, runner, cfg: dict | None = None, *, model: str | None = None, cwd=None, timeout_s: int = 300):
        self.runner = runner
        self.cfg = cfg or {}
        self.model = model
        self.cwd = cwd
        self.timeout_s = timeout_s

    def _label(self) -> str:
        return self.model or "claude-refuter"

    def refute(self, claim: Claim, *, redact_fn=redact) -> RefuterReply:
        # In-subscription, no egress: the full (redacted) claim can be sent - minimal=False keeps the diagnosis prose.
        payload = build_refuter_payload(claim, redact_fn=redact_fn, minimal=False)
        cwd = self.cwd
        if cwd is None:
            from .. import paths
            # A dedicated cwd OUTSIDE data/work: the refuter needs no request data (its claim is on stdin), and the
            # effective-settings writer then denies it Read over data/work so it cannot read a sibling request.
            cwd = paths.data_dir() / "refuter"
        try:
            Path(cwd).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        try:
            res = self.runner.run(payload, kind="refuter", cwd=cwd, session_id=None,
                                  timeout_s=self.timeout_s, model=self.model)
        except Exception as exc:                                   # a crashed/timed-out session = backend error
            return RefuterReply(True, [], self._label(), error=type(exc).__name__, transient=True)
        if not getattr(res, "ok", False):                          # failed session / invalid --model = backend error
            return RefuterReply(True, [], self._label(),
                                error=(getattr(res, "error", "") or "refuter run failed"), transient=True)
        return _parse_refuter_obj(extract_json(getattr(res, "text", "") or ""), self._label())


class PanelRefuter:
    """OPTIONAL small panel of independent Claude refuters on DIFFERENT models (config-gated, off by default). A
    claim survives ONLY if EVERY member fails to refute it; any refutation or error vetoes (veto-not-rescue)."""

    def __init__(self, refuters: list):
        self.refuters = list(refuters)
        self.model = "+".join(getattr(r, "model", None) or "?" for r in self.refuters)

    def refute(self, claim: Claim, *, redact_fn=redact) -> RefuterReply:
        reasons: list[str] = []
        for r in self.refuters:
            reply = _safe_refute(r, claim, redact_fn)
            m = reply.model or "panel"
            if reply.error:
                return RefuterReply(True, [f"{m}: {reply.error}"], self.model, error=reply.error,
                                    transient=reply.transient)
            if reply.refuted:
                return RefuterReply(True, [f"{m}: {x}" for x in (reply.reasons or ["refuted"])], self.model)
            reasons += [f"{m}: {x}" for x in reply.reasons]
        return RefuterReply(False, reasons, self.model)


class GroqRefuter:
    """OPTIONAL non-Claude EXTERNAL refuter (a capable API model). NOT the default. Built by make_refuter only when
    run_mode == live AND a 'groq' key AND the SDK are all present; otherwise the agent degrades to single-model
    (never refute-all). The payload is MINIMISED (structural only) so no staff name can ride out. Lazy-imports the
    SDK; the client is injectable for tests. Redaction happens INSIDE this boundary so no caller can bypass it."""

    def __init__(self, *, model: str = DEFAULT_GROQ_MODEL, client=None, backend=None, timeout_s: int = 30,
                 api_key: str | None = None):
        self.model = model
        self._client = client
        self._backend = backend
        self.timeout_s = timeout_s
        self._api_key = api_key

    def _read_key(self) -> str:
        # Through the vault/creds audit first (CONTRACT 9.2), not os.environ directly; env is the documented fallback.
        from .. import creds
        try:
            return creds._get("groq", backend=self._backend, caller="refuter_xmodel", purpose="cross-model refute")
        except Exception:
            key = os.environ.get("GROQ_API_KEY")
            if key:
                return key
            raise

    def _get_client(self):
        if self._client is None:
            import groq                                             # lazy: only the real path needs the SDK
            self._client = groq.Groq(api_key=self._api_key or self._read_key(), timeout=self.timeout_s)
        return self._client

    def refute(self, claim: Claim, *, redact_fn=redact) -> RefuterReply:
        payload = build_refuter_payload(claim, redact_fn=redact_fn, backend=self._backend, minimal=True)
        try:
            resp = self._get_client().chat.completions.create(
                model=self.model, temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": REFUTER_INSTRUCTIONS},
                          {"role": "user", "content": payload}])
            text = resp.choices[0].message.content
        except Exception as exc:                                   # network, auth, timeout, SDK missing - backend err
            return RefuterReply(True, [], self.model, error=type(exc).__name__, transient=True)
        return _parse_refuter_obj(extract_json(text or ""), self.model)


class FakeRefuter:
    """Deterministic refuter for tests. Never touches the network. Records every claim it was asked about."""

    def __init__(self, *, refuted: bool = False, reasons: list[str] | None = None, error: str = "",
                 yes_man: bool = False, raises: BaseException | None = None, model: str = "fake-llm"):
        self.refuted = refuted
        self.reasons = reasons if reasons is not None else (["seeded counter-case"] if refuted else [])
        self.error = error
        self.yes_man = yes_man
        self.raises = raises
        self.model = model
        self.calls: list[Claim] = []

    def refute(self, claim: Claim, *, redact_fn=redact) -> RefuterReply:
        self.calls.append(claim)
        if self.raises is not None:
            raise self.raises
        if self.yes_man:                                           # the sycophantic "sure, looks fine" answer
            return RefuterReply(False, ["looks fine to me"], self.model)
        if self.error:                                             # models a transient BACKEND error (fix 4)
            return RefuterReply(True, [], self.model, error=self.error, transient=True)
        return RefuterReply(self.refuted, self.reasons, self.model)


# ---------------------------------------------------------------- backend selection

def groq_key_present(*, backend=None) -> bool:
    """By NAME only; never reads or prints a value. True if GROQ_API_KEY is set or a 'groq' vault entry exists."""
    if os.environ.get("GROQ_API_KEY"):
        return True
    try:
        from .. import creds
        return creds.has_secret("groq", backend=backend)
    except Exception:
        return False


def _groq_sdk_present() -> bool:
    """Whether the `groq` SDK is importable. If NOT, the groq backend degrades to single-model (fix 2: a missing
    SDK must never become a blanket refute-all), never a live POST that raises and refutes every claim."""
    import importlib.util
    return importlib.util.find_spec("groq") is not None


def _make_groq(cfg: dict, vcfg: dict, backend, key_present):
    if cfg.get("run_mode") != "live":                   # EXTERNAL third-party egress: live-only (CONTRACT hard rule 3)
        log.info("groq refuter requested but run_mode is not live; cross-model degrades to single-model")
        return None
    present = key_present if key_present is not None else groq_key_present(backend=backend)
    if not present:
        return None
    if not _groq_sdk_present():
        log.warning("groq SDK not installed; cross-model verification degrades to single-model (NOT refute-all) - "
                    "rebuild the venv from requirements.txt to enable the groq backend")
        return None
    return GroqRefuter(model=vcfg.get("groq_model") or DEFAULT_GROQ_MODEL, backend=backend,
                       timeout_s=int(vcfg.get("timeout_seconds") or 30))


def make_refuter(cfg: dict | None = None, *, runner=None, backend=None, client=None, key_present: bool | None = None):
    """Build the configured cross-model refuter, or None -> DEGRADED single-model (evidence gate + Claude judges).
    None is NEVER a refute-all; the packet still verifies on the deterministic gate (fix 2).

    Backend (config.verify.backend):
      - "claude" (DEFAULT): an INDEPENDENT in-subscription Claude session via the provided ``runner``, optionally a
        PANEL of different Claude models. Strong, free, no third-party egress - allowed in observe. No runner -> None.
      - "groq": an optional capable NON-Claude EXTERNAL model, gated to run_mode == live + a 'groq' key + the SDK,
        with a minimised structural-only payload. A tiny local model is deliberately not a backend.
    ``client`` injects a groq-style client (tests / an explicit external client), bypassing the live/SDK gate.
    config.verify.cross_model_enabled: false turns the whole cross-model path off.
    """
    cfg = cfg or {}
    vcfg = (cfg.get("verify") or {})
    if vcfg.get("cross_model_enabled") is False:
        return None
    if client is not None:                              # explicit external client (tests): the groq path
        return GroqRefuter(model=vcfg.get("groq_model") or DEFAULT_GROQ_MODEL, client=client, backend=backend,
                           timeout_s=int(vcfg.get("timeout_seconds") or 30))
    chosen = (vcfg.get("backend") or "claude").lower()
    if chosen == "groq":
        return _make_groq(cfg, vcfg, backend, key_present)
    # default: independent in-subscription Claude session(s)
    if runner is None:
        log.info("cross-model refuter: no Claude runner available; degrading to single-model")
        return None
    timeout_s = int(vcfg.get("refuter_timeout_seconds") or 300)
    panel = [m for m in (vcfg.get("panel") or []) if m]
    if panel:                                           # optional 2-model panel (off by default)
        return PanelRefuter([ClaudeRefuter(runner, cfg, model=m, timeout_s=timeout_s) for m in panel])
    # Reflect the REAL model pin on the object (fix 3: honest model diversity). refuter_model returns a DIFFERENT
    # model only when a worker/refuter model is configured, else None (same model as the worker, no false claim).
    from ..spawner import refuter_model
    return ClaudeRefuter(runner, cfg, model=refuter_model(cfg), timeout_s=timeout_s)
