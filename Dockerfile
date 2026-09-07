FROM python:3.12-slim

WORKDIR /app

# Dépendances système minimales (pdfplumber a besoin de rien de spécial, mais utile pour compat)
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY cm_pdf_to_csv ./cm_pdf_to_csv

RUN pip install --no-cache-dir .

# Le parseur écrit des temp files dans /tmp
ENV PORT=8000
EXPOSE 8000

CMD ["cm-pdf-to-csv"]