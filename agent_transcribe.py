#!/usr/bin/env python3
"""
agent_transcribe.py — minimal "real agent" (framework nélkül)

Features:
- promptok fájlban (specs/prompts) -> nem kódban (review-friendly)
- input: egy PDF vagy egy könyvtár (több PDF)
- persistent agent state (resume)
- autonóm döntés: retry / flag / HITL megállás
- stub könyvtár támogatás: record / replay / generate
- nyelvi beállítás: --lang hu (promptok és policy jellegű küszöbök irányába)

Prereq:
- pdftoppm (poppler)
- (éles mód) openai SDK + OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import base64
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ----------------------------
# Defaults / thresholds (később config fájlba tehetők)
# ----------------------------

DEFAULT_DPI = 300

# Bizonytalanság-jelölők
UNCERTAIN_RE = re.compile(r"\[\?\]")
ILLEGIBLE_RE = re.compile(r"\[\…\]|\[\.\.\.\]")  # […], [...]

# Küszöbök — HU-hoz kicsit engedékenyebbek lehetnek (toldalékolás, hosszú szavak)
DEFAULT_THRESHOLDS = {
    "hu": {"max_uncertain": 50, "max_illegible": 20, "retry_budget": 1},
    "default": {"max_uncertain": 40, "max_illegible": 15, "retry_budget": 1},
}


# ----------------------------
# State
# ----------------------------

@dataclass
class PageState:
    page: int
    image_path: str
    diplomatic_txt_path: Optional[str] = None
    meta_path: Optional[str] = None
    status: str = "pending"  # pending | done | needs_review | failed
    confidence: Optional[str] = None  # low | medium | high
    uncertain_count: int = 0
    illegible_count: int = 0
    retries_used: int = 0
    notes: str = ""


@dataclass
class AgentState:
    source_id: str
    pdf_path: str
    language: str = "hu"
    dpi: int = DEFAULT_DPI
    stage: str = "init"  # init | images_ready | transcribing | v1_ready | normalizing | done
    pages_total: int = 0
    current_page_index: int = 0
    pages: List[PageState] = dataclasses.field(default_factory=list)
    v1_path: Optional[str] = None
    v2_path: Optional[str] = None
    editlog_path: Optional[str] = None
    created_utc: str = ""
    updated_utc: str = ""


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_state(state_path: Path) -> Optional[AgentState]:
    if not state_path.exists():
        return None
    data = json.loads(state_path.read_text(encoding="utf-8"))
    pages = [PageState(**p) for p in data.get("pages", [])]
    data["pages"] = pages
    return AgentState(**data)


def save_state(state: AgentState, state_path: Path) -> None:
    state.updated_utc = utc_now_iso()
    payload = asdict(state)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------
# Helpers
# ----------------------------

def run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sha256_short(path: Path, max_bytes: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        h.update(f.read(max_bytes))
    return h.hexdigest()[:8]


def compute_source_id(pdf_path: Path) -> str:
    # ütközésmentesebb source_id: stem + rövid hash
    return f"{pdf_path.stem}_{sha256_short(pdf_path)}"


def count_markers(text: str) -> Tuple[int, int]:
    return len(UNCERTAIN_RE.findall(text)), len(ILLEGIBLE_RE.findall(text))


def prompt_yes_no(msg: str) -> bool:
    while True:
        ans = input(f"{msg} [y/n] ").strip().lower()
        if ans in ("y", "yes", "i", "igen"):
            return True
        if ans in ("n", "no", "nem"):
            return False


def load_prompt_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing prompt file: {path}")
    return path.read_text(encoding="utf-8")


# ----------------------------
# PDF -> PNG
# ----------------------------

def pdf_to_png(pdf_path: Path, images_dir: Path, source_id: str, dpi: int) -> List[Path]:
    ensure_dir(images_dir)
    prefix = images_dir / f"{source_id}_p"
    run(["pdftoppm", "-png", "-r", str(dpi), str(pdf_path), str(prefix)])

    # pdftoppm általában: prefix-1.png, prefix-2.png...
    generated = sorted(images_dir.glob(f"{source_id}_p-*.png"))
    if not generated:
        generated = sorted(images_dir.glob("*.png"))

    renamed: List[Path] = []
    for i, path in enumerate(sorted(generated), start=1):
        new_name = f"{source_id}_p{i:03d}.png"
        new_path = images_dir / new_name
        if path.name != new_name:
            shutil.move(str(path), str(new_path))
        renamed.append(new_path)

    return renamed


# ----------------------------
# Stub management
# ----------------------------

def stub_paths(stubs_root: Path, kind: str, page_id: str) -> Tuple[Path, Path]:
    """
    kind: 'diplomatic' or 'normalization'
    diplomatic:
      <page_id>.out.txt
      <page_id>.meta.json
    """
    base = stubs_root / kind
    ensure_dir(base)
    out_txt = base / f"{page_id}.out.txt"
    out_meta = base / f"{page_id}.meta.json"
    return out_txt, out_meta


def load_stub_diplomatic(stubs_root: Path, page_id: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    out_txt, out_meta = stub_paths(stubs_root, "diplomatic", page_id)
    if out_txt.exists() and out_meta.exists():
        return out_txt.read_text(encoding="utf-8"), json.loads(out_meta.read_text(encoding="utf-8"))
    return None


def save_stub_diplomatic(stubs_root: Path, page_id: str, transcription: str, meta: Dict[str, Any]) -> None:
    out_txt, out_meta = stub_paths(stubs_root, "diplomatic", page_id)
    out_txt.write_text(transcription, encoding="utf-8")
    out_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def make_generated_stub(source_id: str, page_num: int, page_id: str) -> Tuple[str, Dict[str, Any]]:
    # determinisztikus "fake" kimenet a logika teszteléséhez
    transcription = f"<type>[STUB]</type> {source_id} page {page_num}\n"
    meta = {
        "source_id": source_id,
        "page": page_num,
        "page_id": page_id,
        "confidence": "low",
        "uncertain_markers_count": 0,
        "illegible_spans_count": 0,
        "handwriting_present": False,
        "typewriting_present": True,
        "layout_notes": "generated stub",
        "problems": ["stub_no_model_call"],
    }
    return transcription, meta


# ----------------------------
# Model calls (Responses API)
# ----------------------------

def parse_dual_block_output(raw: str) -> Tuple[str, Dict[str, Any]]:
    """
    Expects:
    === TRANSCRIPTION ===
    ...
    === META ===
    {json}
    """
    if "=== TRANSCRIPTION ===" not in raw or "=== META ===" not in raw:
        raise ValueError("Model output missing required block headers.")
    a = raw.split("=== TRANSCRIPTION ===", 1)[1]
    transcription_part, meta_part = a.split("=== META ===", 1)
    transcription = transcription_part.strip("\n")
    meta_text = meta_part.strip()
    meta = json.loads(meta_text)
    return transcription, meta


def parse_three_block_output(raw: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    if "=== CORRECTED_TEXT ===" not in raw or "=== EDIT_LOG ===" not in raw or "=== META ===" not in raw:
        raise ValueError("Normalization output missing required block headers.")
    a = raw.split("=== CORRECTED_TEXT ===", 1)[1]
    corrected_part, rest = a.split("=== EDIT_LOG ===", 1)
    editlog_part, meta_part = rest.split("=== META ===", 1)
    corrected = corrected_part.strip("\n")
    editlog = json.loads(editlog_part.strip())
    meta = json.loads(meta_part.strip())
    return corrected, editlog, meta


def transcribe_page_openai(prompt_rules: str, image_path: Path, source_id: str, lang: str, page_num: int, page_id: str) -> Tuple[str, Dict[str, Any]]:
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("OpenAI SDK not installed. Run: pip install openai") from e

    client = OpenAI()
    model_id = os.environ.get("TRANSCRIBE_MODEL", "gpt-4.1")

    user_prompt = f"""Language: {lang}
source_id: {source_id}
page: {page_num}
page_id: {page_id}

Task: Produce a diplomatic transcription of this page image.
Do not translate. Use the required output block headers and JSON schema.
"""

    img_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    data_url = f"data:image/png;base64,{img_b64}"
    resp = client.responses.create(
        model=model_id,
        temperature=0.0,
        input=[
            {"role": "system", "content": prompt_rules},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            },
        ],
    )

    transcription, meta = parse_dual_block_output(resp.output_text)
    return transcription, meta


def normalize_v2_openai(prompt_rules: str, v1_text: str, source_id: str, lang: str) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    try:
        from openai import OpenAI
    except Exception as e:
        raise RuntimeError("OpenAI SDK not installed. Run: pip install openai") from e

    client = OpenAI()
    model_id = os.environ.get("NORMALIZE_MODEL", "gpt-4.1")

    user_prompt = f"""Language: {lang}
source_id: {source_id}

Task:
1) Produce corrected/normalized transcription (v2) following rules.
2) Produce EDIT_LOG JSON array.
3) Produce META.

INPUT (v1):
{v1_text}
"""

    resp = client.responses.create(
        model=model_id,
        temperature=0.1,
        input=[
            {"role": "system", "content": prompt_rules},
            {"role": "user", "content": user_prompt},
        ],
    )

    return parse_three_block_output(resp.output_text)


# ----------------------------
# Agent policy decisions
# ----------------------------

def thresholds_for_lang(lang: str) -> Dict[str, int]:
    return DEFAULT_THRESHOLDS.get(lang, DEFAULT_THRESHOLDS["default"])


def should_retry(uncertain: int, illegible: int, retries_used: int, th: Dict[str, int]) -> bool:
    if retries_used >= th["retry_budget"]:
        return False
    return uncertain > th["max_uncertain"] or illegible > th["max_illegible"]


def needs_human_review(confidence: Optional[str], uncertain: int, illegible: int, th: Dict[str, int]) -> bool:
    if confidence == "low":
        return True
    if uncertain > th["max_uncertain"]:
        return True
    if illegible > th["max_illegible"]:
        return True
    return False


# ----------------------------
# Assemble v1
# ----------------------------

def assemble_v1(state: AgentState, out_path: Path) -> None:
    parts: List[str] = []
    parts.append(f"=== SOURCE: {state.source_id} ===\n")
    for ps in sorted(state.pages, key=lambda p: p.page):
        parts.append(f"\n=== PAGE {ps.page} ===\n")
        if not ps.diplomatic_txt_path:
            parts.append("[MISSING PAGE TRANSCRIPTION]\n")
        else:
            parts.append(Path(ps.diplomatic_txt_path).read_text(encoding="utf-8").rstrip() + "\n")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(parts).lstrip(), encoding="utf-8")


# ----------------------------
# Core: run agent for ONE PDF
# ----------------------------

def run_agent_for_pdf(pdf_path: Path, project_root: Path, lang: str, use_api: bool, hitl: bool, stub_mode: str) -> None:
    source_id = compute_source_id(pdf_path)

    # paths
    state_path = project_root / "agent_state" / f"{source_id}.state.json"
    logs_dir = project_root / "logs" / source_id
    ensure_dir(logs_dir)
    log_file = logs_dir / "run.log"

    def log(msg: str) -> None:
        line = f"[{utc_now_iso()}] {msg}"
        print(line)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # prompts
    prompts_dir = project_root / "specs" / "prompts"
    diplomatic_prompt = load_prompt_file(prompts_dir / "diplomatic_transcription_prompt.md")
    normalization_prompt = load_prompt_file(prompts_dir / "normalization_prompt.md")

    th = thresholds_for_lang(lang)

    # load/create state
    state = load_state(state_path)
    if state is None:
        state = AgentState(
            source_id=source_id,
            pdf_path=str(pdf_path),
            language=lang,
            dpi=DEFAULT_DPI,
            stage="init",
            pages_total=0,
            current_page_index=0,
            pages=[],
            created_utc=utc_now_iso(),
            updated_utc=utc_now_iso(),
        )
        save_state(state, state_path)
        log(f"State created: {state_path}")

    work_dir = project_root / "work" / source_id
    images_dir = work_dir / "images"
    diplomatic_dir = work_dir / "diplomatic"
    ensure_dir(images_dir)
    ensure_dir(diplomatic_dir)

    out_dir = project_root / "output" / source_id
    ensure_dir(out_dir)

    stubs_root = project_root / "stubs"

    # Stage: init -> images_ready
    if state.stage == "init":
        log("Stage: PDF -> PNG")
        imgs = pdf_to_png(pdf_path, images_dir, source_id, state.dpi)
        state.pages = [PageState(page=i, image_path=str(p)) for i, p in enumerate(imgs, start=1)]
        state.pages_total = len(state.pages)
        state.stage = "images_ready"
        save_state(state, state_path)
        log(f"Images exported: {state.pages_total} pages")

    # Stage: images_ready -> transcribing
    if state.stage == "images_ready":
        state.stage = "transcribing"
        save_state(state, state_path)
        log("Stage: transcribing")

    # Stage: transcribing
    if state.stage == "transcribing":
        for idx in range(state.current_page_index, state.pages_total):
            ps = state.pages[idx]
            state.current_page_index = idx
            save_state(state, state_path)

            if ps.status == "done":
                continue
            if ps.status == "failed":
                continue
            if ps.status == "needs_review" and not hitl:
                continue

            image_path = Path(ps.image_path)
            page_id = f"{source_id}_p{ps.page:03d}"

            # HITL: previously flagged
            if ps.status == "needs_review" and hitl:
                ok = prompt_yes_no(f"Page {ps.page} flagged for review. Continue (accept as-is) and proceed?")
                if not ok:
                    log("Stopped for manual intervention.")
                    return
                ps.status = "pending"
                save_state(state, state_path)

            # Decide how to get a transcript for this page
            transcription: str
            meta: Dict[str, Any]

            if not use_api:
                # no-api mode -> stub only
                if stub_mode == "replay":
                    loaded = load_stub_diplomatic(stubs_root, page_id)
                    if loaded is None:
                        transcription, meta = make_generated_stub(source_id, ps.page, page_id)
                        save_stub_diplomatic(stubs_root, page_id, transcription, meta)
                        log(f"Stub missing, generated+saved: {page_id}")
                    else:
                        transcription, meta = loaded
                        log(f"Stub replay: {page_id}")
                else:
                    transcription, meta = make_generated_stub(source_id, ps.page, page_id)
                    save_stub_diplomatic(stubs_root, page_id, transcription, meta)
                    log(f"Stub generated+saved: {page_id}")
            else:
                # API mode -> real call
                if stub_mode == "replay":
                    loaded = load_stub_diplomatic(stubs_root, page_id)
                    if loaded is None:
                        raise RuntimeError(f"Stub replay requested but missing stub for {page_id}")
                    transcription, meta = loaded
                    log(f"Stub replay (API suppressed): {page_id}")
                else:
                    transcription, meta = transcribe_page_openai(diplomatic_prompt, image_path, source_id, lang, ps.page, page_id)
                    log(f"Transcribed: page {ps.page} (model call)")
                    if stub_mode == "record":
                        save_stub_diplomatic(stubs_root, page_id, transcription, meta)
                        log(f"Stub recorded: {page_id}")

            # Persist page artifacts
            uncertain, illegible = count_markers(transcription)
            ps.uncertain_count = uncertain
            ps.illegible_count = illegible
            ps.confidence = meta.get("confidence")

            txt_path = diplomatic_dir / f"{page_id}.txt"
            meta_path = diplomatic_dir / f"{page_id}.meta.json"
            txt_path.write_text(transcription, encoding="utf-8")
            meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            ps.diplomatic_txt_path = str(txt_path)
            ps.meta_path = str(meta_path)

            # Agent decision loop: retry? (only meaningful in API mode)
            if use_api and should_retry(uncertain, illegible, ps.retries_used, th):
                ps.retries_used += 1
                ps.notes = f"Retry budget used ({ps.retries_used}/{th['retry_budget']})."
                save_state(state, state_path)
                log(f"Retry triggered for page {ps.page} (u={uncertain}, i={illegible}). Re-run by restarting agent.")
                # egyszerű minimalizmus: most csak flageljük; kifinomultabb verzióban automatikusan újrahívhatjuk
                ps.status = "needs_review" if hitl else "pending"
                save_state(state, state_path)
                if hitl:
                    log("Stopping for HITL after retry trigger.")
                    return

            # Needs human review?
            if needs_human_review(ps.confidence, uncertain, illegible, th):
                ps.status = "needs_review"
                ps.notes = f"Flagged (conf={ps.confidence}, u={uncertain}, i={illegible})"
                save_state(state, state_path)
                log(f"Flagged page {ps.page} for review.")
                if hitl:
                    ok = prompt_yes_no("Stop now for human review? (y=stop, n=continue)")
                    if ok:
                        log("Stopped for human review.")
                        return
                    # ha nem áll meg, akkor elfogadjuk és megyünk tovább
                    ps.status = "done"
                    ps.notes = "Accepted after user chose to continue"
                    save_state(state, state_path)
                    log(f"Continuing; page {ps.page} accepted.")
                else:
                    # HITL off: hagyjuk flagged-ként vagy accepteljük? itt flagged marad
                    pass
            else:
                ps.status = "done"
                ps.notes = "Accepted"
                save_state(state, state_path)

        state.stage = "v1_ready"
        save_state(state, state_path)
        log("Stage: v1_ready")

    # Assemble v1
    if state.stage == "v1_ready":
        v1_path = out_dir / f"{source_id}_diplomatic_v1.txt"
        assemble_v1(state, v1_path)
        state.v1_path = str(v1_path)
        state.stage = "normalizing"
        save_state(state, state_path)
        log(f"v1 assembled: {v1_path}")

    # Normalize v2
    if state.stage == "normalizing":
        if not state.v1_path:
            raise RuntimeError("Missing v1_path in state.")
        v1_text = Path(state.v1_path).read_text(encoding="utf-8")

        if not use_api:
            # stub/no-op normalization
            corrected = v1_text
            editlog: List[Dict[str, Any]] = []
            meta = {"source_id": source_id, "model_id": "stub", "policy_version": "v2", "total_changes": 0, "total_flags": 0, "notes": "stub/no-api"}
        else:
            corrected, editlog, meta = normalize_v2_openai(normalization_prompt, v1_text, source_id, lang)
            log("Normalized v2 (model call)")

        v2_path = out_dir / f"{source_id}_corrected_v2.txt"
        editlog_path = out_dir / f"{source_id}_editlog_v2.json"
        v2_path.write_text(corrected, encoding="utf-8")
        editlog_path.write_text(json.dumps(editlog, ensure_ascii=False, indent=2), encoding="utf-8")

        state.v2_path = str(v2_path)
        state.editlog_path = str(editlog_path)
        state.stage = "done"
        save_state(state, state_path)

        log(f"Done. v1={state.v1_path} v2={state.v2_path} editlog={state.editlog_path}")

        if hitl:
            prompt_yes_no("Normalization done. Approve outputs? (y=finish, n=finish anyway and edit manually later)")


# ----------------------------
# Entry
# ----------------------------

def iter_pdfs(input_path: Path) -> List[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected PDF file: {input_path}")
        return [input_path]
    if input_path.is_dir():
        pdfs = sorted(input_path.glob("*.pdf"))
        if not pdfs:
            raise ValueError(f"No PDFs found in directory: {input_path}")
        return pdfs
    raise ValueError(f"Invalid input path: {input_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=str, help="Path to a PDF or a directory containing PDFs")
    ap.add_argument("--project-root", type=str, default="project", help="Project root directory")
    ap.add_argument("--lang", type=str, default="hu", help="Language code (e.g., hu)")
    ap.add_argument("--no-api", action="store_true", help="Disable OpenAI calls (stub-only)")
    ap.add_argument("--no-hitl", action="store_true", help="Disable human-in-the-loop prompts")
    ap.add_argument("--stub-mode", choices=["off", "record", "replay", "generate"], default="off",
                    help="Stub behavior: off=none, record=save real outputs, replay=use stubs only, generate=always generate+save stubs")
    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()
    input_path = Path(args.input).resolve()

    use_api = not args.no_api
    hitl = not args.no_hitl

    pdfs = iter_pdfs(input_path)
    for pdf in pdfs:
        run_agent_for_pdf(pdf, project_root, args.lang, use_api, hitl, args.stub_mode)


if __name__ == "__main__":
    main()
