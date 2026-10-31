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
SGS_RANGE = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{c}/dados?formato=json&dataInicial={a}&dataFinal={b}"
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
    # Ask for a date range that ends today, so the feed never returns future-dated rows
    # (it now publishes scheduled Copom dates; "last N rows" can be all future).
    start = (NOW.date() - timedelta(days=730)).strftime("%d/%m/%Y")
    end = NOW.date().strftime("%d/%m/%Y")
    rows = get(SGS_RANGE.format(c=code, a=start, b=end))
    today = NOW.date()
    for d in reversed(rows):
        ref_date = datetime.strptime(d["data"], "%d/%m/%Y").date()
        if ref_date <= today:
            return float(d["valor"]), ref_date.isoformat()
    raise RuntimeError(f"SGS {code}: no non-future rows in {start}..{end}")

def _ptax_rows(currency, days, top=None):
    end, start = NOW.date(), NOW.date() - timedelta(days=days)
    f = lambda d: d.strftime("'%m-%d-%Y'")
    if currency == "USD":
        url = PTAX.format(fn="CotacaoDolarPeriodo(dataInicial=@i,dataFinalCotacao=@f)",
                          args=f"@i={f(start)}&@f={f(end)}")
    else:
        url = PTAX.format(fn="CotacaoMoedaPeriodo(moeda=@m,dataInicial=@i,dataFinalCotacao=@f)",
                          args=f"@m='{currency}'&@i={f(start)}&@f={f(end)}")
    if top: url += f"&$top={top}"
    return [r for r in get(url)["value"] if str(r.get("tipoBoletim", "Fechamento")).startswith("Fechamento")]

def ptax(currency):
    r = _ptax_rows(currency, 10)[0]  # newest closing bulletin
    return float(r["cotacaoVenda"]), r["dataHoraCotacao"][:10]

def ptax_history(currency):
    out = {}
    for r in _ptax_rows(currency, 730, top=10000):  # newest first: keep the first (latest) per day
        out.setdefault(r["dataHoraCotacao"][:10], float(r["cotacaoVenda"]))
    return sorted(out.items())

def sgs_history(code, days=730):
    start = (NOW.date() - timedelta(days=days)).strftime("%d/%m/%Y")
    end = NOW.date().strftime("%d/%m/%Y")
    out = []
    for d in get(SGS_RANGE.format(c=code, a=start, b=end)):
        ref = datetime.strptime(d["data"], "%d/%m/%Y").date()
        if ref <= NOW.date(): out.append((ref.isoformat(), float(d["valor"])))
    return out

# IBGE SIDRA, table 6381 (PNAD Contínua), variable 4099: unemployment rate.
# Each row carries D2C = YYYYMM of the last month of the moving quarter.
SIDRA = "https://apisidra.ibge.gov.br/values/t/6381/n1/all/v/4099/p/last%201/d/v4099%201"

# Banco Central, Expectativas de Mercado: median of the Focus survey for IPCA 2026.
FOCUS = ("https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/"
         "ExpectativasMercadoAnuais?$filter=Indicador%20eq%20'IPCA'%20and%20DataReferencia%20eq%20'2026'"
         "&$orderby=Data%20desc&$top=1&$format=json")

def unemployment():
    row = get(SIDRA)[1]  # index 0 is the header row
    # SIDRA gives one column per dimension: D1 territory, D2 variable, D3 period.
    # The period is the only 6-digit code (YYYYMM), so find it by shape, not by position.
    periods = [v for k, v in row.items()
               if k.startswith("D") and k.endswith("C") and isinstance(v, str)
               and len(v) == 6 and v.isdigit()]
    if not periods:
        raise RuntimeError(f"no period code in SIDRA row: {sorted(row)}")
    p = periods[0]
    return float(row["V"]), f"{p[:4]}-{p[4:6]}-01"

def focus_ipca_2026():
    r = get(FOCUS)["value"][0]
    return float(r["Mediana"]), r["Data"][:10]

def unemployment_history():
    rows = get(SIDRA.replace("p/last%201", "p/last%2024"))[1:]  # index 0 is the header row
    out = []
    for row in rows:
        p = next(v for k, v in row.items() if k.startswith("D") and k.endswith("C")
                 and isinstance(v, str) and len(v) == 6 and v.isdigit())
        out.append((f"{p[:4]}-{p[4:6]}-01", float(row["V"])))
    return sorted(out)

def focus_history():
    url = FOCUS.replace("$top=1", "$top=104")
    out = {}
    for r in get(url)["value"]:  # newest first: keep the first row per date
        out.setdefault(r["Data"][:10], float(r["Mediana"]))
    return sorted(out.items())

HISTORY = {  # one-time backfill so charts have a past; the daily job only adds the newest point
    "selic_meta": lambda: sgs_history(432),
    "ipca_12m": lambda: sgs_history(13522),
    "divida_bruta": lambda: sgs_history(13762),
    "ptax_usd": lambda: ptax_history("USD"),
    "ptax_eur": lambda: ptax_history("EUR"),
    "desemprego": unemployment_history,
    "focus_ipca_2026": focus_history,
}

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

def backfill(sid, stamp):
    fn = HISTORY.get(sid)
    if not fn: return "n/a"
    p = f"{OUT}/history/{sid}.csv"
    have = 0
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f: have = sum(1 for _ in f) - 1
    if have >= 20: return "skipped"  # already has a past
    try:
        rows = fn()
        for ref, val in rows: append_history(sid, ref, val, stamp)
        return f"ok:{len(rows)}"
    except Exception as e:  # never fail the run because charts could not be backfilled
        return "failed:" + str(e)[:150]

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
            bf = backfill(sid, stamp)
            append_history(sid, ref, value, stamp)
            status[sid] = {"live_fetch": "success", "run_at": stamp, "last_data": ref, "age_days": age,
                           "history_backfill": bf}
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
