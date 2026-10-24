#!/usr/bin/env python3
"""Brasimundo ingestion v0.1: Banco Central do Brasil open data.

Pulls Selic target, IPCA 12m, PTAX USD and EUR. Python standard library only.
Writes:
  data/latest.json          newest value per series
  data/history/<id>.csv     every ref_date ever seen (deduplicated)
  data/status.json          per-source run log (proof of a live collection)
A source is only "ok" if THIS run fetched it live. Failures never write a guess.
"""
import csv, json, os, time, urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.path.dirname(os.path.abspath(__file__)) + "/data"
NOW = datetime.now(timezone.utc)
UA = {"User-Agent": "brasimundo-ingest/0.1"}
SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{c}/dados/ultimos/{n}?formato=json"
PTAX = ("https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
        "{fn}?{args}&$format=json&$orderby=dataHoraCotacao%20desc")
LIC = "Dados abertos (Banco Central do Brasil)"

def get(url, tries=3):
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network, HTTP, JSON
            err = e; time.sleep(2 * (i + 1))
    raise RuntimeError(f"{type(err).__name__}: {err}")

def sgs(code):
    rows = get(SGS.format(c=code, n=5))
    d = rows[-1]
    ref = datetime.strptime(d["data"], "%d/%m/%Y").date().isoformat()
    return float(d["valor"]), ref

def ptax(currency):
    end, start = NOW.date(), NOW.date() - timedelta(days=10)
    f = lambda d: d.strftime("'%m-%d-%Y'")
    if currency == "USD":
        url = PTAX.format(fn="CotacaoDolarPeriodo(dataInicial=@i,dataFinalCotacao=@f)",
                          args=f"@i={f(start)}&@f={f(end)}")
    else:
        url = PTAX.format(fn="CotacaoMoedaPeriodo(moeda=@m,dataInicial=@i,dataFinalCotacao=@f)",
                          args=f"@m='{currency}'&@i={f(start)}&@f={f(end)}")
    rows = [r for r in get(url)["value"] if str(r.get("tipoBoletim", "Fechamento")).startswith("Fechamento")]
    r = rows[0]  # newest closing bulletin
    return float(r["cotacaoVenda"]), r["dataHoraCotacao"][:10]

SERIES = [  # id, name, unit, frequency, max age in days before "stale", fetcher
    ("selic_meta", "Selic meta (SGS 432)", "% a.a.", "a cada decisão", 60, lambda: sgs(432)),
    ("ipca_12m", "IPCA 12 meses (SGS 13522, origem IBGE)", "%", "mensal", 45, lambda: sgs(13522)),
    ("ptax_usd", "PTAX dólar, venda", "R$", "diária", 5, lambda: ptax("USD")),
    ("ptax_eur", "PTAX euro, venda", "R$", "diária", 5, lambda: ptax("EUR")),
]

def load(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception: return default

def append_history(sid, ref, value, retrieved):
    p = f"{OUT}/history/{sid}.csv"
    seen = set()
    if os.path.exists(p):
        with open(p, newline="", encoding="utf-8") as f:
            seen = {r["ref_date"] for r in csv.DictReader(f)}
    if ref in seen: return
    new = not os.path.exists(p)
    with open(p, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new: w.writerow(["ref_date", "value", "retrieved_at", "verification_level"])
        w.writerow([ref, value, retrieved, "N5"])

def main():
    os.makedirs(f"{OUT}/history", exist_ok=True)
    latest = load(f"{OUT}/latest.json", {})
    status, stamp = {}, NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    for sid, name, unit, freq, max_age, fetch in SERIES:
        try:
            value, ref = fetch()
            age = (NOW.date() - datetime.fromisoformat(ref).date()).days
            latest[sid] = {"name": name, "value": value, "unit": unit, "ref_date": ref,
                           "retrieved_at": stamp, "source": "Banco Central do Brasil",
                           "license": LIC, "verification_level": "N5", "frequency": freq,
                           "data_status": "stale" if age > max_age else "ok"}
            append_history(sid, ref, value, stamp)
            status[sid] = {"live_fetch": "success", "run_at": stamp, "last_data": ref, "age_days": age}
        except Exception as e:
            # keep the previous value, but mark it so nobody mistakes it for fresh
            if sid in latest: latest[sid]["data_status"] = "stale"
            status[sid] = {"live_fetch": "failed", "run_at": stamp, "error": str(e)[:300]}
        print(sid, status[sid]["live_fetch"])
    json.dump(latest, open(f"{OUT}/latest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(status, open(f"{OUT}/status.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
