#!/usr/bin/env python3
"""Build the papers in this repo into PDFs with rxiv-maker.

Each paper folder keeps only its human-authored source (README.md or
MEMEX_WHITEPAPER.md) plus 00_CONFIG.yml / 03_REFERENCES.bib / FIGURES/.
The rxiv body file 01_MAIN.md is a *generated* artifact: this script derives
it from the source markdown, runs `rxiv pdf`, then deletes it again — so
01_MAIN.md never has to live in the repo (it is also .gitignored).

Usage:
    python build.py                # build all papers
    python build.py network-event-transport   # build one paper
    python build.py --keep-main    # keep the generated 01_MAIN.md (for debugging)

Requires: rxiv-maker (`uv tool install rxiv-maker`) and a LaTeX install
(MiKTeX). On Windows the user-scope MiKTeX bin is auto-added to PATH below.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Per-paper configuration.
#   source  : the human-authored markdown that is the source of truth
#   refs    : True if the paper has a manual "## References" section + [N] markers
#             that should become a rxiv-generated bibliography in the PDF
#   note    : markdown reinserted right after the Abstract (content that lived in
#             the source's title block, above ## Abstract, and would otherwise be
#             dropped along with the title/author/date header)
#   columns : 1 (single) or 2 (two-column). Two-column clips wide tables/code, so
#             only the prose-heavy papers are good candidates. Override per run
#             with `python build.py <paper> --columns 2`.
# ---------------------------------------------------------------------------
NET_NOTE = (
    "> **Reading note.** This is a public working draft. All benchmarks measure "
    "*software packet-scheduling overhead* on commodity hardware---not end-to-end "
    "wire latency, NIC latency, or physical propagation. Net does not claim "
    "nanosecond physical networking; it claims that the software coordination path "
    "can be made thin enough that NIC, topology, and physics become the dominant "
    "remaining costs. Real multi-hop network evaluation, formal verification of "
    "partition reconciliation, and hardware-backed attestation remain future work."
)

MEMEX_NOTE = (
    "*A Practical Approach to Epistemic Memory in Financial, Legal, and "
    "Geopolitical Analysis*\n\n"
    "**Library version:** `@ai2070/memex@0.11.0` · "
    "**Status:** Working draft for public release\n\n"
    "> *MemEX is a practical approach to epistemic memory for AI agents, "
    "implementing a subset of properties we have found useful in agent contexts. "
    "It is one point in a broader design space — explored separately in our "
    "forthcoming concept paper, \"Epistemic Memory: A Design Space for "
    "Belief-Aware AI Memory Systems.\"*"
)

PAPERS = {
    "epistemic-resoning-graph-ai-agents": {
        "source": "MEMEX_WHITEPAPER.md",
        "refs": False,
        "note": MEMEX_NOTE,
        "columns": 1,  # wide tables + 118-char code lines -> needs full width
    },
    "intent-broadcasts": {
        "source": "README.md",
        "refs": False,
        "note": None,
        "columns": 1,  # mostly prose; 2 also works well (set --columns 2 to try)
    },
    "network-event-transport": {
        "source": "README.md",
        "refs": True,
        "note": NET_NOTE,
        "columns": 1,  # 72 table rows + wide ASCII diagrams -> needs full width
    },
}


# ---------------------------------------------------------------------------
# Markdown -> rxiv 01_MAIN.md transform
# ---------------------------------------------------------------------------
SPECIALS = {"_": r"\_", "$": r"\$", "~": r"\textasciitilde{}"}


def escape_latex_specials(line: str, in_table: bool) -> str:
    r"""Escape literal LaTeX specials that rxiv leaves raw in prose/tables.

    rxiv's markdown->LaTeX converter does not escape several characters that are
    special to LaTeX. The effect is either a run-on that clips off the page (an
    unescaped `_` or `$` opens subscript/math mode and swallows the following
    spaces, e.g. io_uring or "$500 ...") or a silent mis-render (`~`, meant as
    "approximately", becomes a non-breaking space). None of these papers use
    $...$ math or _italic_ emphasis, so every such character is literal:

        _  ->  \_        $  ->  \$        ~  ->  \textasciitilde{}

    - table rows  : escape everywhere (rxiv won't, even inside backticks here)
    - other lines : escape only OUTSIDE `backtick` spans (rxiv handles those)
    """
    if not any(c in line for c in SPECIALS):
        return line
    if in_table:
        return "".join(SPECIALS.get(ch, ch) for ch in line)
    out, in_code = [], False
    for ch in line:
        if ch == "`":
            in_code = not in_code
            out.append(ch)
        elif ch in SPECIALS and not in_code:
            out.append(SPECIALS[ch])
        else:
            out.append(ch)
    return "".join(out)


def transform(source_text: str, *, refs: bool, note: str | None) -> str:
    """Produce rxiv 01_MAIN.md body from the source markdown."""
    lines = source_text.splitlines()

    # 1. Body starts at the first "## Abstract" (title/author/date header -> config).
    start = next(
        (i for i, ln in enumerate(lines) if ln.lstrip().lower().startswith("## abstract")),
        0,
    )
    body = lines[start:]

    # 2. Drop a trailing manual "## References" section (rxiv generates its own).
    if refs:
        cut = next(
            (i for i, ln in enumerate(body) if ln.strip().lower() == "## references"),
            None,
        )
        if cut is not None:
            body = body[:cut]

    # 3. Reinsert the preserved note right before the first section after Abstract.
    if note:
        ins = next(
            (i for i, ln in enumerate(body[1:], start=1) if ln.startswith("## ")),
            len(body),
        )
        body = body[:ins] + [note, ""] + body[ins:]

    # 4. Per-line fixes: skip fenced code blocks; convert citations; escape `_`.
    out, in_fence = [], False
    for ln in body:
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)
            continue
        if refs:
            ln = re.sub(r"\[(\d+)\]", r"[@ref\1]", ln)
        ln = escape_latex_specials(ln, in_table=ln.lstrip().startswith("|"))
        out.append(ln)

    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Environment / rxiv invocation
# ---------------------------------------------------------------------------
def ensure_environment() -> None:
    """Put a LaTeX toolchain on PATH and make rxiv's Unicode output safe on Windows."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if shutil.which("pdflatex"):
        return
    # Windows user-scope MiKTeX install location.
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/MiKTeX/miktex/bin/x64",
        Path.home() / "AppData/Local/Programs/MiKTeX/miktex/bin/x64",
    ]
    for c in candidates:
        if (c / "pdflatex.exe").exists():
            os.environ["PATH"] = str(c) + os.pathsep + os.environ.get("PATH", "")
            return
    print("WARNING: pdflatex not found on PATH; the build will fail without LaTeX.")


def find_style_cls() -> Path | None:
    """Locate rxiv-maker's rxiv_maker_style.cls on disk.

    We cannot `import rxiv_maker` here: build.py runs under the system Python,
    but rxiv-maker is installed in an isolated uv tool venv. So search the uv
    tool install locations directly (falling back to the import if this ever
    does run under rxiv's interpreter).
    """
    try:
        import rxiv_maker  # noqa: F401

        p = Path(rxiv_maker.__file__).parent / "tex/style/rxiv_maker_style.cls"
        if p.exists():
            return p
    except Exception:
        pass
    roots = []
    if os.environ.get("APPDATA"):
        roots.append(Path(os.environ["APPDATA"]) / "uv/tools")          # Windows
    roots.append(Path.home() / ".local/share/uv/tools")                 # Linux/macOS
    rel = "rxiv_maker/tex/style/rxiv_maker_style.cls"
    for root in roots:
        for pat in (f"*/Lib/site-packages/{rel}", f"*/lib/python*/site-packages/{rel}"):
            for p in root.glob(pat):
                return p
    return None


def patch_rxiv_title_dup() -> None:
    r"""Stop rxiv rendering the auto-synced title as a spurious 'Main' section.

    rxiv-maker syncs the config title into 01_MAIN.md as a leading '# ...' H1.
    That H1 is lead content (before the first '##'), which rxiv drops only when
    the paper also has an 'Introduction' section. For a paper whose first section
    is something else (e.g. intent-broadcasts), the H1 instead renders as
    `\section*{Main}` followed by a duplicate of the title. Patch the section
    splitter to strip that leading auto-synced title from the lead content (the
    title already comes from \maketitle). Idempotent; no-op if rxiv changes.
    """
    cls = find_style_cls()
    if cls is None:
        return
    sp = cls.parents[2] / "converters/section_processor.py"
    if not sp.exists():
        return
    text = sp.read_text(encoding="utf-8")
    if "drop auto-synced title" in text:
        return
    anchor = "    main_content = content[:first_section_start].strip()\n"
    if anchor not in text:
        return
    inject = anchor + (
        "    # drop auto-synced title: rxiv injects the config title as a leading\n"
        "    # '# ...' H1; left in the lead content it renders as a spurious 'Main'\n"
        "    # section + duplicate title when the first section isn't 'Introduction'.\n"
        "    main_content = re.sub(r'^<!--.*?-->\\s*', '', main_content, flags=re.DOTALL).strip()\n"
        "    main_content = re.sub(r'^# .*(?:\\n|$)', '', main_content).strip()\n"
    )
    sp.write_text(text.replace(anchor, inject, 1), encoding="utf-8")


def set_columns(n: int) -> None:
    """Patch rxiv's style class to 1 or 2 columns (idempotent).

    rxiv-maker's rxiv_maker_style.cls forces two columns in two places: the
    default in \\ExecuteOptions AND a hardcoded \\twocolumn later. Two columns
    clip these papers' wide tables and ASCII diagrams off the page, so we drive
    the layout from here. The hardcoded \\twocolumn is rewritten to respect the
    class option; the option default is then set to the requested column count.
    Re-applies automatically if rxiv is ever reinstalled.
    """
    cls = find_style_cls()
    if cls is None or not cls.exists():
        print("  WARNING: could not locate rxiv_maker_style.cls; column layout unchanged")
        return
    want = "twocolumn" if n == 2 else "onecolumn"
    other = "onecolumn" if n == 2 else "twocolumn"
    text = cls.read_text(encoding="utf-8")
    patched = text.replace(
        "\n\\twocolumn \\sloppy \\flushbottom",
        "\n\\if@tmptwocolumn\\twocolumn\\else\\onecolumn\\fi \\sloppy \\flushbottom",
    ).replace(
        f"\\ExecuteOptions{{times,twoside,{other}}}",
        f"\\ExecuteOptions{{times,twoside,{want}}}",
    )
    if patched != text:
        cls.write_text(patched, encoding="utf-8")


def build_paper(folder: str, *, keep_main: bool, columns: int | None = None) -> bool:
    cfg = PAPERS[folder]
    paper = REPO / folder
    src = paper / cfg["source"]
    if not src.exists():
        print(f"  SKIP {folder}: source {cfg['source']} not found")
        return False

    set_columns(columns if columns is not None else cfg.get("columns", 1))

    main = paper / "01_MAIN.md"
    main.write_text(
        transform(src.read_text(encoding="utf-8"), refs=cfg["refs"], note=cfg["note"]),
        encoding="utf-8",
        newline="\n",
    )
    try:
        proc = subprocess.run(
            ["rxiv", "pdf", folder, "--skip-validation"],
            cwd=REPO,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        ok = proc.returncode == 0 and (paper / "output" / f"{folder}.pdf").exists()
        if ok:
            print(f"  OK   {folder} -> {folder}/output/{folder}.pdf")
        else:
            print(f"  FAIL {folder} (rxiv exit {proc.returncode})")
            tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-15:])
            print("    " + tail.replace("\n", "\n    "))
        return ok
    finally:
        if not keep_main and main.exists():
            main.unlink()


def main(argv: list[str]) -> int:
    keep_main = "--keep-main" in argv
    columns = None
    if "--columns" in argv:
        i = argv.index("--columns")
        columns = int(argv[i + 1])
        del argv[i : i + 2]
    names = [a for a in argv if not a.startswith("--")]
    targets = names or list(PAPERS)
    unknown = [t for t in targets if t not in PAPERS]
    if unknown:
        print(f"Unknown paper(s): {', '.join(unknown)}")
        print(f"Available: {', '.join(PAPERS)}")
        return 2

    ensure_environment()
    patch_rxiv_title_dup()
    print(f"Building {len(targets)} paper(s)...")
    results = [build_paper(t, keep_main=keep_main, columns=columns) for t in targets]
    n_ok = sum(results)
    print(f"\nDone: {n_ok}/{len(results)} built.")
    return 0 if n_ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
