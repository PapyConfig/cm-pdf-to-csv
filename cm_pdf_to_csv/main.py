"""cm-pdf-to-csv — API de conversion des extraits Crédit Mutuel (PDF) en transactions.

Enveloppe credit_mutuel_pdf_extractor (exécuté en sous-processus pour profiter de sa
validation de soldes) et expose une API HTTP simple, consommable par n8n.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

logger = logging.getLogger("cm_pdf_to_csv")
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="cm-pdf-to-csv", version="0.2.0")

CMUT_BIN = os.environ.get("CMUT_BIN", "cmut_process_pdf")

# Correspondance no. compte (via external_id) -> Securo account_id.
# NB: les comptes doivent exister dans Securo (créés à la main ou via /api/accounts).
DEFAULT_ACCOUNT_MAP: dict[str, str] = {
    "00019360702": "1a94239c-1bcb-4149-98ca-17876a3cec2b",  # Compte Courant
    "00019360760": "d9deb12e-4eec-4f35-a4f1-88e8ad99071b",  # Livret Bleu
}


def _run_cmut(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """Lance le parseur CM en sous-processus et renvoie les transactions brutes."""
    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "extrait.pdf"
        pdf.write_bytes(pdf_bytes)
        out = Path(tmp) / "out.json"
        proc = subprocess.run(
            [CMUT_BIN, str(pdf), "--output", str(out)],
            capture_output=True, text=True, timeout=120,
        )
        if proc.returncode != 0:
            raise HTTPException(status_code=422, detail=f"Parsing failed: {proc.stderr[-1000:]}")
        if not out.exists():
            raise HTTPException(
                status_code=422,
                detail=f"Parseur n'a rien produit. stdout: {proc.stdout[-500:]} stderr: {proc.stderr[-500:]}",
            )
        return json.loads(out.read_text())


def _build_response(raw: list[dict[str, Any]]) -> dict[str, Any]:
    """Regroupe les transactions par compte avec contrôle de solde."""
    by_account: dict[str, list[dict]] = {}
    for tx in raw:
        by_account.setdefault(tx["Account"], []).append(tx)

    accounts = []
    for acc, txs in by_account.items():
        txs_sorted = sorted(txs, key=lambda t: t["Date"])
        accounts.append({
            "account": acc,
            "transactions": [
                {
                    "date": t["Date"],
                    "description": t["Description"],
                    "amount": str(t["Amount"]),
                    "amount_float": t["Amount"],
                }
                for t in txs_sorted
            ],
        })

    return {"accounts": accounts, "total_transactions": len(raw)}


def _to_securo_csv(acc_txs: list[dict[str, Any]]) -> str:
    """Rend un CSV au format attendu par Securo (date,description,amount)."""
    import csv
    import io

    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["date", "description", "amount"])
    for t in acc_txs:
        writer.writerow([t["date"], t["description"], t["amount"]])
    return "\n".join(buf.getvalue().splitlines())


def _load_account_map() -> dict[str, str]:
    """Laisse env SECURO_ACCOUNT_MAP (JSON path) surcharger le défaut."""
    path = os.environ.get("SECURO_ACCOUNT_MAP")
    if not path:
        return DEFAULT_ACCOUNT_MAP
    return json.loads(Path(path).read_text())


@app.post("/api/convert")
async def convert(file: UploadFile = File(...)):
    """Reçoit un extrait PDF, renvoie les transactions par compte (JSON)."""
    data = await file.read()
    if not data or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="A PDF file is required.")
    raw = _run_cmut(data)
    resp = _build_response(raw)
    resp["file"] = file.filename
    return JSONResponse(resp)


@app.post("/api/convert/csv")
async def convert_csv(file: UploadFile = File(...)):
    """Reçoit un extrait PDF, renvoie un CSV au format Securo par compte.

    Format de retour: {"accounts": {<no_compte>: "<csv>"}, "total": N}
    """
    data = await file.read()
    if not data or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="A PDF file is required.")
    raw = _run_cmut(data)
    resp = _build_response(raw)

    csvs = {}
    for acc in resp["accounts"]:
        acc_no = acc["account"]
        csvs[acc_no] = _to_securo_csv(acc["transactions"])
    return JSONResponse({"accounts": csvs, "total": resp["total_transactions"]})


@app.post("/api/securo/import-preview")
async def securo_import_preview(
    file: UploadFile = File(...),
    account_map_path: str | None = Query(None, description="JSON mapping no_compte -> account_id (optionnel)"),
):
    """Parse le PDF côté service, puis appelle le preview d'import Securo.

    Réponse: {previews: {<account_id>: <reponse preview Securo>}, errors: [...]}
    """
    base = _securo_base()
    data = await file.read()
    raw = _run_cmut(data)
    resp = _build_response(raw)
    account_map = _load_account_map()

    token = _securo_token(base)
    headers = {"Authorization": f"Bearer {token}"}

    previews, errors = {}, []
    for acc in resp["accounts"]:
        acc_no = acc["account"]
        account_id = account_map.get(acc_no)
        if not account_id:
            errors.append({"account": acc_no, "error": "no account_id mapping"})
            continue
        csv_content = _to_securo_csv(acc["transactions"])
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{base}/api/transactions/import/preview",
                headers=headers,
                files={"file": (f"cm_{acc_no}.csv", csv_content, "text/csv")},
            )
        if r.status_code != 200:
            errors.append({"account": acc_no, "status": r.status_code, "error": r.text[:500]})
        else:
            previews[account_id] = r.json()

    return JSONResponse({"previews": previews, "errors": errors})


def _securo_base() -> str:
    base = os.environ.get("SECURO_BASE_URL", "http://10.42.1.101:3000")
    return base.rstrip("/")


def _build_import_payload(acc_txs: list[dict[str, Any]], account_id: str, filename: str) -> dict:
    """Construit le payload d'import Securo (TransactionImportRequest)."""
    txs = []
    for t in acc_txs:
        amt = float(t["amount"])
        txs.append({
            "description": t["description"],
            "amount": str(abs(amt)),
            "date": t["date"],
            "type": "debit" if amt < 0 else "credit",
        })
    return {
        "account_id": account_id,
        "transactions": txs,
        "filename": filename,
        "detected_format": "csv",
        "detect_duplicates": True,
    }


_SECURO_TOKEN_CACHE: dict[str, str] = {}
_SECURO_TOKEN_EXPIRY: dict[str, float] = {}
# JWT OAuth2 Securo — purge courte (Securo rate-limite /api/auth/login, 429 en rafale).
# Un login toutes les ~600s max, même avec N imports simultanés.
_SECURO_TOKEN_TTL: float = float(os.environ.get("SECURO_TOKEN_TTL", "600"))


def _securo_token(base: str) -> str:
    """Token d'accès Securo (OAuth2 password flow), avec cache anti-rate-limit.

    Plusieurs imports concurrents (multi-PDF via n8n) partagent le même token :
    sans cache, chaque appel fait un login → Securo répond 429 Too Many Requests.
    """
    import time

    now = time.time()
    cached = _SECURO_TOKEN_CACHE.get(base)
    if cached and _SECURO_TOKEN_EXPIRY.get(base, 0.0) > now:
        return cached

    email = os.environ.get("SECURO_EMAIL", "support@rohmes.fr")
    password = os.environ.get("SECURO_PASSWORD", "L4GP4f&YN0mIfl")
    r = httpx.post(
        f"{base}/api/auth/login",
        data={"username": email, "password": password},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    _SECURO_TOKEN_CACHE[base] = token
    _SECURO_TOKEN_EXPIRY[base] = time.time() + _SECURO_TOKEN_TTL
    return token


def _securo_token_reset(base: str) -> None:
    """Force le renouvellement du token après un 401 (expiration réelle)."""
    _SECURO_TOKEN_CACHE.pop(base, None)
    _SECURO_TOKEN_EXPIRY.pop(base, None)


async def _securo_import_bytes(data: bytes, filename: str, base: str) -> dict:
    """Parse un PDF puis importe les transactions dans Securo (commit)."""
    raw = _run_cmut(data)
    resp = _build_response(raw)
    account_map = _load_account_map()

    token = _securo_token(base)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    results, errors = [], []
    async with httpx.AsyncClient(timeout=90) as client:
        for acc in resp["accounts"]:
            acc_no = acc["account"]
            account_id = account_map.get(acc_no)
            if not account_id:
                errors.append({"account": acc_no, "error": "no account_id mapping"})
                continue
            payload = _build_import_payload(acc["transactions"], account_id, filename)
            r = await client.post(f"{base}/api/transactions/import", headers=headers, json=payload)
            if r.status_code == 401:
                # Token expiré (TTL cache dépassé mais JWT déjà mort) → renew + retry une fois
                _securo_token_reset(base)
                headers = {"Authorization": f"Bearer {_securo_token(base)}", "Content-Type": "application/json"}
                r = await client.post(f"{base}/api/transactions/import", headers=headers, json=payload)
            if r.status_code not in (200, 201):
                errors.append({"account": acc_no, "status": r.status_code, "error": r.text[:500]})
            else:
                body = r.json()
                results.append({"account": acc_no, "account_id": account_id, **body})

    return {"results": results, "errors": errors}


@app.post("/api/securo/import")
async def securo_import(file: UploadFile = File(...)):
    """Parse le PDF (multipart) puis importe dans Securo (commit)."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Aucun contenu reçu")
    result = await _securo_import_bytes(data, file.filename or "extrait.pdf", _securo_base())
    return JSONResponse(result)


@app.post("/api/securo/import/raw")
async def securo_import_raw(request: Request):
    """Import depuis un PDF envoyé en body brut (Content-Type: application/pdf)."""
    data = await request.body()
    if not data:
        raise HTTPException(status_code=422, detail="Empty body")
    result = await _securo_import_bytes(data, "cm_raw.pdf", _securo_base())
    return JSONResponse(result)


@app.post("/api/securo/import/base64")
async def securo_import_base64(payload: dict):
    """Import depuis un PDF encodé en base64 dans un JSON: {"filename", "data"}.

    Chemin privilégié par n8n (formulaire) : le runner JS ne sait envoyer
    ni multipart ni binaire brut proprement, mais le JSON/base64 est trivial.
    """
    data_b64 = payload.get("data") or payload.get("file")
    filename = payload.get("filename", "extrait.pdf")
    if not data_b64:
        raise HTTPException(status_code=422, detail="Champ 'data' (base64) manquant")
    try:
        data = base64.b64decode(data_b64)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Base64 invalide: {e}")
    if not data:
        raise HTTPException(status_code=422, detail="Contenu vide après décodage")
    return await _securo_import_bytes(data, filename, base_url := _securo_base())


@app.get("/api/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))