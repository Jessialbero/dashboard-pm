#!/usr/bin/env python3
"""Consulta Jira, lee config/sow.json y config/pm-input.yml, y genera site/data.json.
Solo usa la librería estándar. Variables: JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN (y ASOF opcional, AAAA-MM-DD)."""
import base64, json, math, os, pathlib, re, urllib.parse, urllib.request
from datetime import date, timedelta as TD

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOW = json.loads((ROOT/"config/sow.json").read_text(encoding="utf-8"))
JC = SOW["jira"]
STATE = {"closed": "Cerrado", "active": "En curso"}
d10 = lambda s: date.fromisoformat(s[:10])

def pm_input():
    o = {}
    for l in (ROOT/"config/pm-input.yml").read_text(encoding="utf-8").splitlines():
        l = l.strip()
        if l and not l.startswith("#") and ":" in l:
            k, v = l.split(":", 1)
            o[k.strip()] = re.sub(r"\s+#.*$", "", v.strip()).strip("\"'")
    return o

def jira(path, **q):
    url = os.environ["JIRA_BASE_URL"].rstrip("/") + path + ("?" + urllib.parse.urlencode(q) if q else "")
    tok = base64.b64encode(f'{os.environ["JIRA_EMAIL"]}:{os.environ["JIRA_TOKEN"]}'.encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + tok, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)

def paged(path, key, **q):
    out, start = [], 0
    while True:
        r = jira(path, startAt=start, maxResults=100, **q)
        v = r.get(key, []); out += v; start += len(v)
        if not v or r.get("isLast") or ("total" in r and start >= r["total"]): return out

def wd(a, b):
    o, d = [], a
    while d <= b:
        if d.weekday() < 5: o.append(d)
        d += TD(1)
    return o

def nextwd(d):
    d += TD(1)
    while d.weekday() >= 5: d += TD(1)
    return d

def burn(days, items, tot, asof):
    ideal = [[i, round(tot*(1-i/len(days)), 1)] for i in range(len(days)+1)]
    act = [[0, tot]] + [[i+1, tot-sum(t["sp"] for t in items if t["done"] and t["done"] <= d)]
                        for i, d in enumerate(days) if d <= asof]
    return ideal, act

def main():
    pm = pm_input()
    sprints = [s for s in paged(f"/rest/agile/1.0/board/{JC['board_id']}/sprint", "values", state="active,closed,future")
               if s["name"].startswith(JC["sprint_prefix"]) and s.get("startDate") and s.get("endDate")]
    sprints.sort(key=lambda s: s["startDate"])
    T = {}
    for i, s in enumerate(sprints, 1):
        for it in paged(f"/rest/agile/1.0/sprint/{s['id']}/issue", "issues", fields="*all"):
            f = it["fields"]; cat = f["status"]["statusCategory"]["key"]
            flag = any(isinstance(v, list) and v and isinstance(v[0], dict) and v[0].get("value") == "Impediment" for v in f.values())
            T[it["key"]] = dict(key=it["key"], summary=f["summary"], s=i, sp=int(f.get(JC["sp_field"]) or 0),
                                type=f["issuetype"]["name"], status=f["status"]["name"],
                                done=d10(f["statuscategorychangedate"]) if cat == "done" else None,
                                flagged=flag and cat != "done", since=d10(f["updated"]))
    tasks = list(T.values())
    START, END = d10(sprints[0]["startDate"]), d10(sprints[-1]["endDate"])
    asof = min(max(date.fromisoformat(os.environ.get("ASOF") or str(date.today())), START), END)

    total = sum(t["sp"] for t in tasks); done_sp = sum(t["sp"] for t in tasks if t["done"]); rem = total - done_sp
    elapsed = len(wd(START, asof)); pace = done_sp/elapsed if elapsed else 0
    fend = asof if rem == 0 else None
    if rem and pace:
        fend = asof
        for _ in range(math.ceil(rem/pace)): fend = nextwd(fend)
    delay = (fend-END).days if fend else 0
    days = wd(START, max(END, fend or END)); plan_n = len(wd(START, END)); now = elapsed

    spr, planned_sp, ms, bands = [], 0, {}, []
    for i, s in enumerate(sprints, 1):
        a, b = d10(s["startDate"]), d10(s["endDate"]); ts = [t for t in tasks if t["s"] == i]
        tot = sum(t["sp"] for t in ts); dn = sum(t["sp"] for t in ts if t["done"])
        w = wd(a, b); fr = len([d for d in w if d <= asof])/len(w) if w else 1
        planned_sp += tot*fr
        active = s["state"] == "active"
        behind = active and ((tot-dn) > tot*(1-fr)+0.01 or any(t["flagged"] for t in ts))
        ms[i] = ("closed" if s["state"] == "closed" and dn == tot else "late" if b < asof and dn < tot else "risk" if behind else "ok")
        spr.append(dict(name=s["name"], start=str(a), end=str(b), planned=tot, done=dn, state=STATE.get(s["state"], "Planificado")))
        bands.append([len(wd(START, a-TD(1))), len(wd(START, b)), f"S{i}"])
    cur = next((i for i, s in enumerate(sprints) if s["state"] == "active"),
               max([i for i, s in enumerate(sprints) if s["state"] == "closed"], default=0))
    cs = sprints[cur]; ct = [t for t in tasks if t["s"] == cur+1]
    d2 = wd(d10(cs["startDate"]), d10(cs["endDate"])); i2, a2 = burn(d2, ct, sum(t["sp"] for t in ct), asof)
    _, act = burn(days, tasks, total, asof)
    ideal = [[i, round(total*max(0, 1-i/plan_n), 1)] for i in range(len(days)+1)]

    blocked = [dict(key=t["key"], summary=t["summary"], since=str(t["since"]),
                    reason=pm.get("bloqueo_"+t["key"], "Marcado como impedimento en Jira.")) for t in tasks if t["flagged"]]
    spi = round(done_sp/planned_sp, 2) if planned_sp else 1.0
    sem = "verde" if delay <= 0 else "amarillo" if delay <= 14 else "rojo"
    if blocked and sem == "verde": sem = "amarillo"
    reasons = [f"Fin proyectado {delay} días después del plan" if delay > 0 else "Fin proyectado dentro del plan",
               f"{len(blocked)} issue(s) bloqueado(s)", f"Ritmo al {int(spi*100)}% de lo planificado"]

    MS = {"closed": ("Cumplido", "verde"), "late": ("Atrasado", "rojo"), "risk": ("En riesgo", "amarillo"), "ok": ("Pendiente", "mute")}
    milestones = []
    for m in SOW["milestones"]:
        st, col = MS[ms.get(m["sprint"], "ok")]
        milestones.append(dict(id=m["id"], name=m["name"], date=m["date"], status=st, color=col))
    ok = {m["id"] for m in milestones if m["status"] == "Cumplido"}
    invoiced = sum(p["amount"] for p in SOW["payments"] if p["after"] == "firma" or p["after"] in ok)
    risks = []
    for r in SOW["risks"]:
        st = pm.get("riesgo_"+r["id"], "Abierto")
        risks.append(dict(r, status=st, color={"Materializado": "rojo", "Mitigado": "verde"}.get(st, "mute")))
    base = SOW["budget"]["base"]
    data = dict(
        project=dict(name=SOW["project"]["name"], client=SOW["project"]["client"], asOf=str(asof), end=str(END)),
        status=dict(auto=sem, override=pm.get("semaforo") if pm.get("semaforo") in ("verde", "amarillo", "rojo") else None, reasons=reasons),
        pm=dict(comment=pm.get("comentario") or "Sin comentario del PM por ahora."),
        kpis=dict(total_sp=total, done_sp=done_sp, pct=round(100*done_sp/total) if total else 0, forecast_end=str(fend or END),
                  delay=delay, blocked=len(blocked), spi=str(spi).replace(".", ",")),
        budget=dict(base=base, earned=round(base*done_sp/total) if total else 0, planned=round(base*planned_sp/total) if total else 0,
                    invoiced=invoiced, show_amounts=pm.get("mostrar_montos", "false").lower() in ("true", "si", "sí", "1")),
        burndownProject=dict(x=["inicio"]+[str(d) for d in days], now=now, bands=bands, ideal=ideal, actual=act,
                             forecast=[[now, rem], [len(days), 0]] if fend else []),
        sprintChart=dict(name=cs["name"], x=["inicio"]+[str(d) for d in d2], now=len([d for d in d2 if d <= asof]), ideal=i2, actual=a2),
        tasks=[{k: t[k] for k in ("key", "s", "sp", "type", "status")} for t in tasks],
        sprints=spr, blocked=blocked, milestones=milestones, risks=risks)
    out = ROOT/"site"; out.mkdir(exist_ok=True)
    (out/"data.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {len(tasks)} issues, {done_sp}/{total} SP, estado {sem}, fin proyectado {fend}")

if __name__ == "__main__":
    main()
