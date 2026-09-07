# cm-pdf-to-csv

Convertit les extraits de compte **Crédit Mutuel** (PDF) en transactions structurées, prêtes à importer dans **Securo** (ou tout autre outil).

Service HTTP léger (FastAPI) qui enveloppe [`credit_mutuel_pdf_extractor`](https://github.com/maximehk/credit_mutuel_pdf_extractor) — la lib open-source mit qui parse les relevés CM : dates, libellés, montants, détection multi-comptes et **validation des soldes** début/fin du relevé.

## Pourquoi

- Le Crédit Mutuel n'expose pas d'export CSV/OFX dans toutes les caisses → les extraits sont en PDF.
- Securo (self-hosted, ho-dock-1) importe CSV/OFX/QIF/CAMT via API, mais pas de PDF brut.
- Ce service comble le maillon : **PDF → CSV/JSON prêt pour Securo**, utilisable par n8n (workflow d'import automatique) ou manuellement.

## Usage

### API

```
POST /api/convert
Content-Type: multipart/form-data
file: <extrait.pdf>
```

Réponse : `200` avec les transactions par compte :

```json
{
  "file": "Extrait_2026-08-25.pdf",
  "accounts": [
    {
      "account": "00019360702",
      "type": "checking",
      "transactions": [
        {"date": "2026-07-29", "description": "PAIEMENT CB 2807 RUE PLEYEL, S", "amount": "-44.18"}
      ],
      "start_balance": "143.40",
      "end_balance": "61.49",
      "balance_validated": true
    }
  ],
  "balance_ok": true
}
```

## Endpoint d'import Securo

```
POST /api/securo/import-preview          → transmet tel quel au backend Securo (multipart)
POST /api/securo/import                  → parse + post directement dans Securo
```

Requiert `SECURO_BASE_URL` (ex. `http://10.42.1.101:3000`) et les identifiants via `SECURO_EMAIL` / `SECURO_PASSWORD` (token récupéré automatiquement au lancement).

## Développement

```bash
uv venv && uv sync
uv run uvicorn cm_pdf_to_csv.main:app --reload --port 8000
curl -F "file=@extrait.pdf" http://localhost:8000/api/convert
```

## Docker

```bash
docker build -t cm-pdf-to-csv:local .
docker run -p 8000:8000 -e SECURO_BASE_URL=http://10.42.1.101:3000 cm-pdf-to-csv:local
```