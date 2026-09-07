"""CLI entry point: uvicorn launcher so `cm-pdf-to-csv` works from the shell."""
from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "cm_pdf_to_csv.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )


if __name__ == "__main__":
    main()