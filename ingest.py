#!/usr/bin/env python3
"""Brasimundo ingestion v0.1: Banco Central do Brasil open data.

Pulls Selic, IPCA 12m, PTAX USD/EUR, unemployment (IBGE), gross debt and the Focus IPCA 2026 median. Python standard library only.
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
    today = NOW.date()
    # newest row that is not dated in the future (a future date means a scheduled entry, not a fact)
    for d in reversed(rows):
        ref_date = datetime.strptime(d["data"], "%d/%m/%Y").date()
        if ref_date <= today:
            return float(d["valor"]), ref_date.isoformat()
    raise RuntimeError(f"SGS {code}: no non-future rows returned")

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

# IBGE SIDRA, table 6381 (PNAD Contínua), variable 4099: unemployment rate.
# Each row carries D2C = YYYYMM of the last month of the moving quarter.
SIDRA = "https://apisidra.ibge.gov.br/values/t/6381/n1/all/v/4099/p/last%201/d/v4099%201"

# Banco Central, Expectativas de Mercado: median of the Focus survey for IPCA 2026.
FOCUS = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
         "ExpectativasMercadoAnuais?$filter=Indicador%20eq%20'IPCA'%20and%20DataReferencia%20eq%20'2026'"
         "&$orderby=Data%20desc&$top=1&$format=json")

def unemployment():
    row = get(SIDRA)[1]  # index 0 is the header row
    p = row["D2C"]
    return float(row["V"]), f"{p[:4]}-{p[4:6]}-01"

def focus_ipca_2026():
    r = get(FOCUS)["value"][0]
    return float(r["Mediana"]), r["Data"][:10]

SERIES = [  # id, name, unit, frequency, max age in days before "stale", fetcher
    ("selic_meta", "Selic meta (SGS 432)", "% a.a.", "a cada decisão", 60, lambda: sgs(432)),
    # monthly series are dated on the 1st of the reference month and released ~10 days later,
    # so 75 days is the realistic limit before it is really stale.
    # Unemployment and gross debt publish about two months after the reference month: 100 days.
    ("ipca_12m", "IPCA 12 meses (SGS 13522, origem IBGE)", "%", "mensal", 75, lambda: sgs(13522)),
    ("ptax_usd", "PTAX dólar, venda", "R$", "diária", 5, lambda: ptax("USD")),
    ("ptax_eur", "PTAX euro, venda", "R$", "diária", 5, lambda: ptax("EUR")),
    ("desemprego", "Taxa de desocupação (PNAD Contínua, IBGE)", "%", "trimestre móvel", 100, unemployment),
    ("divida_bruta", "Dívida bruta do governo geral (SGS 13762)", "% do PIB", "mensal", 100, lambda: sgs(13762)),
    ("focus_ipca_2026", "Focus: IPCA 2026, mediana", "%", "semanal", 14, focus_ipca_2026),
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
    if not any(s["live_fetch"] == "success" for s in status.values()):
        raise SystemExit("ALL sources failed; see data/status.json")  # red X in GitHub Actions

if __name__ == "__main__":
    main()
