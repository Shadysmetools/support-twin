"""ANALYSE (stage 3): classify a request into issue-vs-feature + the Odoo convention fields + a root-cause read.

Deterministic and keyword-driven (NO LLM call in CI — the real production ANALYSE can swap in a model behind the
same function signature; the Judge (§16) then scores the classification). Fed by the TWO knowledge sources
(Odoo + Wiki ``client_context``): a module already associated with this client biases the module guess.

Outputs map onto Supervisor's conventions (plan §14.1, verified against the live field set at write time):
  - ticket_type : Bug | New Request | Customization | Configuration | Inquiries   (-> x_type id)
  - track       : inquiry | functional | development                              (-> x_track selection)
"""
from __future__ import annotations

import re

from .model import Analysis, detect_lang
from .slots import request_summary

# Module glossary (EN + AR), order matters: first hit wins.
_MODULES = [
    ("Inventory", ("inventory", "stock", "warehouse", "مخزون", "مخازن")),
    ("Sales", ("sales", "sale order", "quotation", "مبيعات", "عرض سعر", "اوردر")),
    ("Purchase", ("purchase", "vendor bill", "rfq", "مشتريات", "مورد")),
    ("Accounting", ("accounting", "invoice", "journal", "payment", "محاسبة", "فاتورة", "قيد")),
    ("POS", ("pos", "point of sale", "نقطة بيع", "كاشير")),
    ("Manufacturing", ("manufacturing", "mrp", "bom", "تصنيع", "أمر تصنيع")),
    ("HR", ("hr", "employee", "payroll", "attendance", "موظف", "مرتبات", "حضور")),
    ("Helpdesk", ("helpdesk", "ticket", "support", "دعم", "تذكرة")),
    ("Website", ("website", "web", "ecommerce", "متجر", "موقع")),
    ("CRM", ("crm", "lead", "pipeline", "عميل محتمل")),
]

_ISSUE_HINTS = ("error", "not working", "doesn't work", "broken", "bug", "fail", "crash", "wrong", "can't",
                "cannot", "مشكلة", "مش شغال", "مش بيشتغل", "خطأ", "عطل", "مابيفتحش", "واقع")
_FEATURE_HINTS = ("add", "feature", "could you", "can you add", "we need", "request", "new report", "customize",
                  "please build", "عايز", "محتاج", "ممكن تضيفوا", "خاصية", "تقرير جديد", "نضيف")
_CONFIG_HINTS = ("configure", "setup", "set up", "settings", "enable", "permission", "access", "إعداد", "تفعيل",
                 "صلاحية", "ضبط")
_CUSTOM_HINTS = ("customize", "custom field", "modify the", "change the layout", "tweak", "تعديل", "تخصيص",
                 "حقل مخصص")
_INQUIRY_HINTS = ("how do i", "how to", "what is", "where is", "question", "?", "إزاي", "ازاي", "يعني ايه",
                  "فين", "ازى", "استفسار")
_URGENT_HINTS = ("urgent", "urgently", "asap", "immediately", "right now", "production is down", "system is down",
                 "عاجل", "ضروري", "ضروري جدا", "حالا", "متوقف", "واقف", "الشغل واقف")
_EVIDENCE_HINTS = ("http://", "https://", "screenshot", "screen shot", "attached", "صورة", "سكرين", "لينك", "فيديو")


def _hit(low: str, key: str) -> bool:
    """Whole-word match for plain ASCII keywords ("hr" must not hit "through", "add" must not hit "address");
    keys with punctuation or Arabic letters ("?", "http://", Arabic words) stay substring matches."""
    k = key.lower()
    if k[:1].isascii() and k[:1].isalnum() and k[-1:].isascii() and k[-1:].isalnum():
        return re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low) is not None
    return k in low


def _any(low: str, keys) -> bool:
    return any(_hit(low, k) for k in keys)


def _first_module(text: str) -> str:
    low = (text or "").lower()
    for name, keys in _MODULES:
        if _any(low, keys):
            return name
    return "unknown"


def _has_evidence(text: str) -> bool:
    return _any((text or "").lower(), _EVIDENCE_HINTS)


# TODO(Riley/Supervisor): review the Arabic wording below (the open-question and outcome strings are new in the Arabic
# CONFIRM message; Template 2/2b text itself is copied verbatim from plan v2 §13).
_Q_AR = {"evidence": "لقطة شاشة مع رابط الصفحة، أو تسجيل شاشة قصير، عشان نقدر نعيد المشكلة",
         "module": "أنهي شاشة أو موديول المقصود"}
_Q_EN = {"evidence": "a screenshot with the page URL, or a short screen recording, so we can reproduce it",
         "module": "which screen or module this is about"}
_OUTCOME = {"en": {"issue": "it to work as expected", "feature": "this capability added"},
            "ar": {"issue": "الموضوع يشتغل زي ما هو متوقع", "feature": "نضيف الخاصية دي"}}


def question_en(q: str) -> str:
    """The English twin of an open question (the ticket for the team is always English; the client-facing CONFIRM
    draft keeps the client's language). An unknown question is returned unchanged."""
    for key, ar in _Q_AR.items():
        if q == ar:
            return _Q_EN[key]
    return q


def classify(text: str, *, client_context: dict | None = None) -> Analysis:
    """Pure classifier. ``client_context`` (from the two knowledge sources) nudges the module guess only."""
    low = (text or "").strip().lower()
    lang = detect_lang(text)
    issue = _any(low, _ISSUE_HINTS)
    feature = _any(low, _FEATURE_HINTS)
    nature = "issue" if (issue and not feature) else ("feature" if feature else "issue")

    if _any(low, _CONFIG_HINTS):
        ticket_type, track = "Configuration", "functional"
    elif _any(low, _CUSTOM_HINTS):
        ticket_type, track = "Customization", "development"
    elif nature == "feature":
        ticket_type, track = "New Request", "development"
    elif _any(low, _INQUIRY_HINTS) and not issue:
        ticket_type, track = "Inquiries", "inquiry"
    else:
        ticket_type, track = "Bug", "functional"

    module = _first_module(text)
    if module == "unknown":
        ctx_modules = (client_context or {}).get("modules") or []
        if len(ctx_modules) == 1:                    # a single known module for this client is a safe nudge
            module = str(ctx_modules[0])

    questions: list[str] = []
    if nature == "issue" and not _has_evidence(text):
        questions.append((_Q_AR if lang == "ar" else _Q_EN)["evidence"])
    if module == "unknown":
        questions.append((_Q_AR if lang == "ar" else _Q_EN)["module"])

    conf = 0.75 if module != "unknown" else 0.5
    if nature == "issue" and not _has_evidence(text):
        conf -= 0.1
    conf = round(max(0.1, min(0.95, conf)), 2)

    summary = request_summary(text)                  # the request, description labels removed, cut on a whole word
    outcome = _OUTCOME[lang][nature]
    root = {"issue": "needs reproduction on staging before a fix can be scoped",
            "feature": "scope and effort to be confirmed with the owner"}[nature]

    return Analysis(nature=nature, ticket_type=ticket_type, track=track, module=module, root_cause=root,
                    scope=("single module" if module != "unknown" else "unscoped"), open_questions=questions,
                    confidence=conf, summary=summary or "(voice note / empty)", desired_outcome=outcome,
                    urgency="High" if _any(low, _URGENT_HINTS) else "not stated")


def analyse(text: str, *, lang: str = "", client_context: dict | None = None) -> Analysis:
    """Stage-3 entry point. ``lang`` is informational; classification is language-agnostic (EN+AR keyword sets)."""
    _ = lang or detect_lang(text)
    return classify(text, client_context=client_context)
