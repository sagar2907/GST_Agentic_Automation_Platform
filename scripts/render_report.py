"""Render docs/report.md to a PDF.

This machine has no LaTeX, no pandoc and no wkhtmltopdf, so the route is
markdown -> styled HTML -> headless Chrome's print-to-PDF. Chrome is a
dependency worth naming explicitly rather than discovering at render time.

Two failure modes are handled deliberately:

* Chrome dies with "Multiple targets are not supported in headless mode" when
  the input path contains spaces, so the file URL is percent-encoded and the
  PDF is written to a temporary space-free directory before being copied.
* The first invocation can silently produce nothing unless it is given its own
  user-data directory.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import pathname2url

import markdown

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs" / "report.md"
OUTPUT = REPO_ROOT / "docs" / "GST_ITC_Reconciliation_Report.pdf"

CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path("/usr/bin/google-chrome"),
    Path("/usr/bin/chromium"),
)

STYLE = """
@page { size: A4; margin: 18mm 16mm 20mm 16mm; }
body {
  font-family: Georgia, "Times New Roman", serif;
  font-size: 10.5pt; line-height: 1.55; color: #17181c; max-width: 100%;
}
h1 { font-size: 21pt; margin: 0 0 6pt; padding-bottom: 6pt;
     border-bottom: 2.5px solid #17181c; page-break-after: avoid; }
h2 { font-size: 15pt; margin: 22pt 0 6pt; padding-bottom: 3pt;
     border-bottom: 1px solid #c7c9d1; page-break-after: avoid; }
h3 { font-size: 12pt; margin: 15pt 0 4pt; page-break-after: avoid; }
h1 + h3 { font-style: italic; color: #4a4d57; border: 0; margin-top: 2pt; }
p, li { orphans: 3; widows: 3; }
code, pre { font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; }
code { font-size: 9pt; background: #f2f3f6; padding: 1px 3px; border-radius: 3px; }
pre { background: #f7f8fa; border: 1px solid #dfe1e7; border-left: 3px solid #6b6f7d;
      padding: 8pt 10pt; font-size: 8.6pt; line-height: 1.4; overflow-x: auto;
      page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }
table { border-collapse: collapse; width: 100%; margin: 10pt 0; font-size: 9.2pt;
        page-break-inside: avoid; }
th, td { border: 1px solid #d3d5dd; padding: 4.5pt 7pt; text-align: left;
         vertical-align: top; }
th { background: #eef0f4; font-weight: bold; }
blockquote { margin: 10pt 0; padding: 6pt 12pt; border-left: 3px solid #17181c;
             background: #f7f8fa; font-style: italic; page-break-inside: avoid; }
hr { border: 0; border-top: 1px solid #c7c9d1; margin: 20pt 0; }
strong { font-weight: bold; }
"""


def find_chrome() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    raise SystemExit(
        "No Chrome or Edge binary found. This renderer needs one of:\n  "
        + "\n  ".join(str(path) for path in CHROME_CANDIDATES)
    )


def build_html(md_text: str) -> str:
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "sane_lists", "attr_list"],
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n'
        '<meta charset="utf-8">\n'
        "<title>GST ITC Reconciliation</title>\n"
        f"<style>{STYLE}</style>\n</head>\n<body>\n{body}\n</body>\n</html>\n"
    )


def render(html: str, output: Path) -> Path:
    chrome = find_chrome()
    # A space-free working directory: Chrome's headless mode misparses paths
    # containing spaces and exits with an unrelated-sounding error.
    with tempfile.TemporaryDirectory(prefix="gstrecon-pdf-") as workdir:
        work = Path(workdir)
        html_path = work / "report.html"
        pdf_path = work / "report.pdf"
        html_path.write_text(html, encoding="utf-8")
        url = "file:" + pathname2url(str(html_path))
        result = subprocess.run(
            [
                str(chrome),
                "--headless=new",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--user-data-dir={work / 'profile'}",
                f"--print-to-pdf={pdf_path}",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=240,
            check=False,
        )
        if not pdf_path.exists():
            raise SystemExit(
                f"Chrome produced no PDF (exit {result.returncode}).\n{result.stderr[:800]}"
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pdf_path, output)
    return output


def main() -> int:
    if not SOURCE.exists():
        raise SystemExit(f"missing {SOURCE}")
    html = build_html(SOURCE.read_text(encoding="utf-8"))
    output = render(html, OUTPUT)
    print(f"wrote {output} ({output.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
