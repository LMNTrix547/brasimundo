#!/usr/bin/env python3
"""Brasimundo ingestion v0.1: Banco Central do Brasil open data.

Pulls Selic, IPCA 12m, PTAX USD/EUR, unemployment (IBGE, national and by state), gross debt and the Focus IPCA 2026 median. Python standard library only.
Writes:
  data/latest.json          newest value per series
  data/history/<id>.csv     every ref_date ever seen (deduplicated)
  data/status.json          per-source run log (proof of a live collection)
A source is only "ok" if THIS run fetched it live. Failures never write a guess.
"""
import csv, json, os, re, time, urllib.request
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

# Unemployment for each of the 27 states (IBGE SIDRA, table 6381, territory level UF).
# SIDRA uses the IBGE state code, so map it to the two-letter abbreviation.
STATE_UF = {"11":"RO","12":"AC","13":"AM","14":"RR","15":"PA","16":"AP","17":"TO",
            "21":"MA","22":"PI","23":"CE","24":"RN","25":"PB","26":"PE","27":"AL","28":"SE","29":"BA",
            "31":"MG","32":"ES","33":"RJ","35":"SP","41":"PR","42":"SC","43":"RS",
            "50":"MS","51":"MT","52":"GO","53":"DF"}
# State unemployment (27 UFs). Sources are tried in order; the first that returns all 27 states wins.
# Table 6468 is IBGE's PNAD Contínua table for state-level data (variable 4099, the unemployment rate).
# Every failure is recorded in status.json with what IBGE actually returned.
STATE_SOURCES = [
    ("SIDRA 6468", "https://apisidra.ibge.gov.br/values/t/6468/n3/all/v/4099/p/last%201", "sidra"),
    ("SIDRA 6381", "https://apisidra.ibge.gov.br/values/t/6381/n3/all/v/4099/p/last%201", "sidra"),
    ("IBGE agregados 6381", "https://servicodados.ibge.gov.br/api/v3/agregados/6381/periodos/-1/variaveis/4099?localidades=N3[all]", "agregados"),
]

def _states_from_sidra(rows):
    # The column holding the state code is not always D1C: with several territorial levels SIDRA
    # numbers the columns differently. So look for the first D<n>C column whose value is a UF code.
    out, codes_seen = {}, set()
    for row in rows[1:]:  # index 0 is the header row
        uf_col = next((k for k, v in row.items() if re.fullmatch(r"D\d+C", k)
                       and str(v) in STATE_UF), None)
        if uf_col is None: continue
        uf = STATE_UF[str(row[uf_col])]; codes_seen.add(str(row[uf_col]))
        try:
            value = float(row["V"])
        except (TypeError, ValueError):
            continue  # unpublished value (SIDRA uses symbols); the count check below catches the gap
        p = next((v for k, v in row.items() if re.fullmatch(r"D\d+C", k)
                  and isinstance(v, str) and len(v) == 6 and v.isdigit()), None)
        if p is None: raise RuntimeError(f"no period code for {uf}")
        out[uf] = {"name": row.get(uf_col.replace("C", "N"), uf), "value": value,
                   "ref_date": f"{p[:4]}-{p[4:6]}-01"}
    if len(out) != 27:
        cols = sorted({k for r in rows[1:4] for k in r})
        raise RuntimeError(f"{len(out)} states from {len(rows) - 1} rows; state codes seen {sorted(codes_seen)[:6]}; "
                           f"columns {cols[:14]}")
    return out

def _states_from_agregados(data):
    out = {}
    series = data[0]["resultados"][0]["series"]
    for s in series:
        uf = STATE_UF.get(str(s["localidade"]["id"]))
        if uf is None: continue
        valid = {k: v for k, v in s["serie"].items() if v not in (None, "", "-", "...")}
        if not valid: continue
        period = max(valid)  # YYYYMM keys: the latest period sorts last
        out[uf] = {"name": s["localidade"].get("nome", uf), "value": float(valid[period]),
                   "ref_date": f"{period[:4]}-{period[4:6]}-01"}
    if len(out) != 27:
        raise RuntimeError(f"{len(out)} states from {len(series)} series")
    return out

def states_unemployment():
    errors = []
    for label, url, kind in STATE_SOURCES:
        try:
            data = get(url, tries=2)
            states = _states_from_agregados(data) if kind == "agregados" else _states_from_sidra(data)
            bad = [uf for uf, x in states.items() if not in_range("desemprego", x["value"])]
            if bad: raise RuntimeError(f"implausible values for {bad}")
            return states
        except Exception as e:
            errors.append(f"{label}: {str(e)[:200]}")
    raise RuntimeError(" | ".join(errors))

HISTORY = {  # one-time backfill so charts have a past; the daily job only adds the newest point
    "selic_meta": lambda: sgs_history(432),
    "ipca_12m": lambda: sgs_history(13522),
    "divida_bruta": lambda: sgs_history(13762),
    "ptax_usd": lambda: ptax_history("USD"),
    "ptax_eur": lambda: ptax_history("EUR"),
    "desemprego": unemployment_history,
    "focus_ipca_2026": focus_history,
}

# Banco Central, balança comercial (bens), Balanço de Pagamentos, mensal, US$ milhões (SGS 22707).
# The indicator is the year-to-date balance: the sum of the monthly values for the latest year.
BALANCA_SGS = 22707

def balanca_ytd():
    rows = sgs_history(BALANCA_SGS)
    if not rows: raise RuntimeError("no rows from SGS 22707")
    year = max(d[:4] for d, _ in rows)
    ytd = [(d, v) for d, v in rows if d[:4] == year]
    return sum(v for _, v in ytd), max(d for d, _ in ytd)

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
    ("balanca_comercial", "Balança comercial, acumulado no ano (SGS 22707)", "US$ milhões", "mensal", 75, balanca_ytd),
]

# Plausible range per series. A value outside it is rejected as a failed fetch, so a wrong
# series code or unit cannot reach the site as a current number. Wide on purpose: this only
# catches gross errors, not ordinary movements.
PLAUSIBLE = {
    "selic_meta": (0, 40),
    "ipca_12m": (-5, 40),
    "ptax_usd": (1, 20),
    "ptax_eur": (1, 25),
    "desemprego": (0, 40),
    "divida_bruta": (20, 150),   # % do PIB
    "focus_ipca_2026": (-5, 40),
    "balanca_comercial": (-100000, 200000),  # US$ milhões, year to date
}

def in_range(sid, value):
    lo, hi = PLAUSIBLE.get(sid, (float("-inf"), float("inf")))
    return lo <= value <= hi

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
        rows = [(ref, val) for ref, val in fn() if in_range(sid, val)]
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
            if not in_range(sid, value):
                raise RuntimeError(f"implausible value {value} for {sid}; check the series code and unit")
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
    # State unemployment: its own file, replaced only when all 27 states arrive.
    try:
        states = states_unemployment()
        bad = [uf for uf, s_ in states.items() if not in_range("desemprego", s_["value"])]
        if bad: raise RuntimeError(f"implausible state values: {bad}")
        json.dump({"retrieved_at": stamp, "source": "IBGE, PNAD Contínua (desocupação por UF)",
                   "license": "Dados abertos (IBGE)", "verification_level": "N5",
                   "series": "desemprego", "states": states},
                  open(f"{OUT}/estados.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        status["estados_desemprego"] = {"live_fetch": "success", "run_at": stamp, "states": len(states)}
    except Exception as e:
        status["estados_desemprego"] = {"live_fetch": "failed", "run_at": stamp, "error": str(e)[:900]}
    print("estados_desemprego", status["estados_desemprego"]["live_fetch"])
    json.dump(latest, open(f"{OUT}/latest.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(status, open(f"{OUT}/status.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not any(s["live_fetch"] == "success" for s in status.values()):
        raise SystemExit("ALL sources failed; see data/status.json")  # red X in GitHub Actions

if __name__ == "__main__":
    main()
