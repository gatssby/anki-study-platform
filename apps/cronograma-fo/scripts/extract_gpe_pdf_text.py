from __future__ import annotations

import csv
from pathlib import Path

from pypdf import PdfReader


PROJECT_ROOT = Path(__file__).resolve().parent.parent

PDFS = [
    ("MAT", PROJECT_ROOT / "GPE Matemática.pdf"),
    ("FIS", PROJECT_ROOT / "GPE Física.pdf"),
]

OUTPUT_CSV = PROJECT_ROOT / "work" / "gpe_bridge" / "staging" / "gpe_pdf_pages.csv"


def clean_text(text: str) -> str:
    return (
        text.replace("\x00", " ")
        .replace("\r", "\n")
        .strip()
    )


def main() -> None:
    rows = []

    for subject, pdf_path in PDFS:
        reader = PdfReader(str(pdf_path))

        for idx, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            text = clean_text(text)

            rows.append(
                {
                    "subject": subject,
                    "pdf_name": pdf_path.name,
                    "page": idx,
                    "text": text,
                }
            )

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["subject", "pdf_name", "page", "text"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"CSV gerado com {len(rows)} páginas:")
    print(OUTPUT_CSV)


if __name__ == "__main__":
    main()
