"""LOCAL frame/screenshot OCR seam for the proof pipeline (15 step 7 privacy).

The kit's ``scripts/privacy.py`` scans TEXT only; the proof video's frames/screenshots carry text that must also be
scanned for secrets before delivery. This module turns image paths into text, OFFLINE and LOCAL ONLY - never a cloud
or network OCR, and it NEVER fabricates text. If no local engine is installed it says so (raises ``NoOCREngine``),
so ``RealReproducer`` keeps ``frame_texts`` None and ``run()``'s privacy step stays FAIL-CLOSED.

``ocr_images(paths, engine=None)`` is the whole public surface. ``engine`` is an injectable callable
``image_path -> str`` so tests pass a fake and no real OCR binary/network is ever touched; when it is ``None`` the
engine is resolved from the real LOCAL engines, in this preference order (all offline):

  1. Windows OCR (``Windows.Media.Ocr``) via ``winsdk`` or ``winrt`` - pip-only, NO external binary.
  2. ``pytesseract`` - only when a ``tesseract`` binary is actually on PATH.

Nothing here reaches the network, and no cloud OCR is ever called.
"""
from __future__ import annotations

import shutil
import threading
from typing import Callable, List, Optional, Sequence

Engine = Callable[[str], str]

VALID_PREFERENCES = ("auto", "windows", "tesseract", "none")


class NoOCREngine(RuntimeError):
    """No LOCAL OCR engine is available. The caller must FAIL CLOSED (do not fabricate frame text)."""


def ocr_images(paths: Sequence[str], engine: Optional[Engine] = None) -> List[str]:
    """OCR each image path to one text (blank string if that image yields nothing). Returns one entry per input,
    in order, so the pipeline's per-item coverage check stays aligned.

    ``engine`` is an injectable ``image_path -> str`` callable (tests pass a fake). When ``None``, a real LOCAL
    engine is resolved; if none is available this raises ``NoOCREngine`` so the pipeline stays fail-closed. A
    per-image read/engine error is NOT swallowed into a blank - it propagates, so the caller returns no partial list.
    """
    paths = list(paths or [])
    if not paths:
        return []                      # nothing to OCR; do not resolve an engine for an empty capture
    if engine is None:
        engine = resolve_engine("auto")
    if engine is None:
        raise NoOCREngine("no local OCR engine available (install winsdk/winrt or a tesseract binary)")
    return [str(engine(p) or "") for p in paths]


def resolve_engine(prefer: str = "auto") -> Optional[Engine]:
    """Resolve a real LOCAL OCR engine (a ``path -> str`` callable), or ``None`` when none is available.

    ``prefer`` comes from ``proof.ocr_engine``: ``auto`` (Windows then tesseract), ``windows``, ``tesseract`` or
    ``none``. Any UNKNOWN value is treated as ``none`` (fail closed), never ``auto``. Engine libraries are imported
    lazily and defensively here, so importing this module (or ``pipeline``) never touches them and resolution can
    never crash the run."""
    prefer = str(prefer or "auto").strip().lower()
    if prefer not in VALID_PREFERENCES:
        return None                    # unknown preference: fail closed, do not silently OCR with the wrong engine
    if prefer == "none":
        return None
    if prefer in ("auto", "windows"):
        eng = _windows_engine()
        if eng is not None:
            return eng
        if prefer == "windows":
            return None
    if prefer in ("auto", "tesseract"):
        eng = _tesseract_engine()
        if eng is not None:
            return eng
    return None


def _windows_engine() -> Optional[Engine]:
    """Windows.Media.Ocr via winsdk (preferred) or the older winrt. Pip-only, offline, no external binary. Returns
    a ``path -> str`` callable, or None if the package / OCR language pack is unavailable."""
    mods = _import_winrt()
    if mods is None:
        return None
    ocr_mod, imaging_mod, storage_mod = mods
    try:
        ocr_engine = ocr_mod.OcrEngine.try_create_from_user_profile_languages()
    except Exception:                  # noqa: BLE001 - any winrt failure => treat as unavailable, never crash
        return None
    if ocr_engine is None:             # no installed OCR language pack
        return None

    async def _recognize(path: str) -> str:
        file = await storage_mod.StorageFile.get_file_from_path_async(path)
        stream = await file.open_async(storage_mod.FileAccessMode.READ)
        decoder = await imaging_mod.BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await ocr_engine.recognize_async(bitmap)
        return result.text or ""

    def engine(path: str) -> str:
        # recognize_async must run on an event loop; the proof worker may already be inside one, so run the
        # coroutine on a FRESH loop in a dedicated thread (asyncio.run would raise if a loop is already running).
        return _run_coro_blocking(_recognize(path))

    return engine


def _import_winrt():
    """Import the Windows.Media.Ocr trio from winsdk or winrt. None if neither is importable."""
    for pkg in ("winsdk", "winrt"):
        try:
            ocr_mod = __import__(f"{pkg}.windows.media.ocr", fromlist=["OcrEngine"])
            imaging_mod = __import__(f"{pkg}.windows.graphics.imaging", fromlist=["BitmapDecoder"])
            storage_mod = __import__(f"{pkg}.windows.storage", fromlist=["StorageFile", "FileAccessMode"])
            return ocr_mod, imaging_mod, storage_mod
        except Exception:              # noqa: BLE001 - not installed / wrong platform => try the next, then None
            continue
    return None


def _run_coro_blocking(coro):
    """Run an async coroutine to completion on a fresh event loop in a worker thread and return its result.
    Avoids 'event loop is already running' if the caller is itself async."""
    import asyncio

    result: dict = {}

    def runner():
        loop = asyncio.new_event_loop()
        try:
            result["value"] = loop.run_until_complete(coro)
        except BaseException as e:     # noqa: BLE001 - surface it to the calling thread
            result["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    t.join(timeout=60)                 # a hung winrt call must not stall the worker forever (unlike _run_kit's timeout)
    if t.is_alive():
        raise TimeoutError("OCR engine timed out")
    if "error" in result:
        raise result["error"]
    return result.get("value", "")


def _tesseract_engine() -> Optional[Engine]:
    """pytesseract, but ONLY when a real ``tesseract`` binary is on PATH (the Python package alone cannot OCR).
    Returns a ``path -> str`` callable, or None if either piece is missing."""
    if shutil.which("tesseract") is None:
        return None
    try:
        import pytesseract
        from PIL import Image
    except Exception:                  # noqa: BLE001 - package missing => unavailable
        return None

    def engine(path: str) -> str:
        with Image.open(path) as im:
            return pytesseract.image_to_string(im) or ""

    return engine
