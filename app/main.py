"""Impact evidence platform (lean core). Provenance-first: every value has a source; unknown stays NULL."""
from dotenv import load_dotenv
load_dotenv()  # reads .env from the folder you run uvicorn in; real environment variables take priority
import os, io, json, math, sqlite3, uuid, datetime as dt
from pathlib import Path
import numpy as np
from PIL import Image
from fastapi import Response, FastAPI, UploadFile, File, HTTPException, Request
from . import cloud, ai
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Impact Evidence Platform")
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,filename TEXT,path TEXT,sha256 TEXT,
 captured_at TEXT,captured_at_source TEXT DEFAULT 'UNKNOWN',lat REAL,lng REAL,location_source TEXT DEFAULT 'UNKNOWN',
 cloud_id TEXT UNIQUE,public_id TEXT,secure_url TEXT,resource_type TEXT DEFAULT 'image',fmt TEXT,version TEXT,tags TEXT DEFAULT '[]',phash TEXT,quality TEXT,colors TEXT,
 stage TEXT DEFAULT 'UNKNOWN',stage_source TEXT DEFAULT 'UNKNOWN',summary TEXT,embedding TEXT,status TEXT DEFAULT 'UPLOADED');
CREATE TABLE IF NOT EXISTS relations(a TEXT,b TEXT,type TEXT,score REAL,signals TEXT,UNIQUE(a,b,type));
CREATE TABLE IF NOT EXISTS pairs(id TEXT PRIMARY KEY,project_id TEXT,before_id TEXT,after_id TEXT,confidence REAL,
 status TEXT DEFAULT 'needs_review',UNIQUE(before_id,after_id));
CREATE TABLE IF NOT EXISTS derived(id TEXT PRIMARY KEY,media_id TEXT,public_id TEXT,version TEXT,purpose TEXT,url TEXT,ts TEXT);
CREATE TABLE IF NOT EXISTS audit(id TEXT PRIMARY KEY,ts TEXT,action TEXT,target TEXT,old TEXT,new TEXT,reason TEXT);
"""

def data_dir(): return Path(os.getenv("DATA_DIR", "data"))
def db():
    d = data_dir(); (d / "uploads").mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(d / "app.db"); c.row_factory = sqlite3.Row; c.executescript(SCHEMA)
    for t, col in (('media', 'analysis TEXT'), ('pairs', 'changes TEXT')):
        try: c.execute(f'ALTER TABLE {t} ADD COLUMN {col}')
        except sqlite3.OperationalError: pass
    return c
def uid(): return uuid.uuid4().hex
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def audit(c, action, target, old, new, reason=None):
    c.execute("INSERT INTO audit VALUES(?,?,?,?,?,?,?)", (uid(), now(), action, target, str(old), str(new), reason))

def _deg(v, ref):
    d, m, s = (float(x) for x in v); r = d + m / 60 + s / 3600
    return -r if ref in ("S", "W") else r

def read_exif(img):
    """Returns (captured_at|None, lat|None, lng|None). Never falls back to file/upload time."""
    ex = img.getexif(); when = lat = lng = None
    raw = ex.get_ifd(0x8769).get(36867) or ex.get(306)
    if raw:
        try: when = dt.datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError: pass
    g = ex.get_ifd(0x8825)
    if 2 in g and 4 in g:
        try: lat, lng = _deg(g[2], g.get(1, "N")), _deg(g[4], g.get(3, "E"))
        except (TypeError, ValueError): pass
    return when, lat, lng

def embed(img):
    t = np.asarray(img.convert("RGB").resize((16, 16)), dtype=np.float32).ravel(); t = t - t.mean()
    n = np.linalg.norm(t); return (t / n if n else t)

def km(a, b, c, d):
    p = math.pi / 180; x = math.sin((c - a) * p / 2) ** 2 + math.cos(a * p) * math.cos(c * p) * math.sin((d - b) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(x))

def pscore(m, o):
    sim = float(np.dot(np.array(json.loads(m["embedding"])), np.array(json.loads(o["embedding"]))))
    sig = {"visual": (max(sim, 0), 0.6)}
    if None not in (m["lat"], m["lng"], o["lat"], o["lng"]):
        d = km(m["lat"], m["lng"], o["lat"], o["lng"]); sig["location"] = (1.0 if d < .1 else .7 if d < 1 else .2, .4)
    return sim, sig, sum(v * w for v, w in sig.values()) / sum(w for _, w in sig.values())

def discover(c, m):
    """Score peers using only signals that exist; infer missing location from a strong visual match (flagged, never EXIF)."""
    if not m["embedding"]: return
    for o in c.execute("SELECT * FROM media WHERE project_id=? AND id!=? AND embedding IS NOT NULL", (m["project_id"], m["id"])).fetchall():
        o = dict(o); sim, sig, score = pscore(m, o)
        a, b = sorted([m["id"], o["id"]])
        typ = "DUPLICATE" if sim > .98 else "POSSIBLE_SAME_SITE" if score >= .7 and len(sig) > 1 else None
        if typ:
            c.execute("INSERT OR REPLACE INTO relations VALUES(?,?,?,?,?)", (a, b, typ, score, json.dumps({k: v[0] for k, v in sig.items()})))
        if typ == "POSSIBLE_SAME_SITE" and m["captured_at"] and o["captured_at"] and m["captured_at"] != o["captured_at"]:
            bef, aft = sorted([m, o], key=lambda r: r["captured_at"])
            c.execute("INSERT OR IGNORE INTO pairs(id,project_id,before_id,after_id,confidence,status) VALUES(?,?,?,?,?,'needs_review')", (uid(), m["project_id"], bef["id"], aft["id"], score))
        if sim >= .85:
            for x, y in ((m, o), (o, m)):
                if x["lat"] is None and y["lat"] is not None and x["location_source"] == "UNKNOWN":
                    x["lat"], x["lng"], x["location_source"] = y["lat"], y["lng"], "RELATION_INFERRED"
                    c.execute("UPDATE media SET lat=?,lng=?,location_source='RELATION_INFERRED' WHERE id=?", (y["lat"], y["lng"], x["id"]))
                    audit(c, "location_inferred", x["id"], None, f"from {y['id']} sim={sim:.2f}")

class Named(BaseModel): name: str
class PhaseIn(BaseModel): phase: str; reason: str | None = None
class CaptureIn(BaseModel): captured_at: str; reason: str | None = None
class ReviewIn(BaseModel): status: str
class AskIn(BaseModel): question: str

@app.post("/api/projects", status_code=201)
def create_project(p: Named):
    pid = uid()
    with db() as c: c.execute("INSERT INTO projects VALUES(?,?)", (pid, p.name))
    return {"id": pid, "name": p.name}

@app.get("/api/projects")
def projects():
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM projects")]

def need_project(c, pid):
    if not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone(): raise HTTPException(404, "project not found")

def ingest(c, pid, **v):
    mid = uid(); need_project(c, pid)
    cols = dict(id=mid, project_id=pid, captured_at_source="EXIF" if v.get("captured_at") else "UNKNOWN",
                location_source="EXIF" if v.get("lat") is not None else "UNKNOWN", status="ENRICHED" if v.get("embedding") else "EMBEDDING_FAILED", **v)
    c.execute(f"INSERT INTO media({','.join(cols)}) VALUES({','.join('?'*len(cols))})", list(cols.values()))
    discover(c, dict(c.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone())); audit(c, "ingest", mid, None, v.get("filename"))
    return mid

def ingest_cloudinary(pid, p):
    """Shared by the webhook and /uploads/complete. Idempotent on Cloudinary asset_id."""
    cid = p.get("asset_id") or p.get("public_id")
    with db() as c:
        ex = c.execute("SELECT id FROM media WHERE cloud_id=?", (cid,)).fetchone()
        if ex: return ex["id"], False
    md = p.get("image_metadata") or {}; when = None
    try: when = dt.datetime.strptime(str(md.get("DateTimeOriginal")), "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError: pass          # Cloudinary created_at is upload time - never used as capture time
    lat, lng = cloud.parse_gps(md.get("GPSLatitude"), md.get("GPSLatitudeRef")), cloud.parse_gps(md.get("GPSLongitude"), md.get("GPSLongitudeRef"))
    m = dict(secure_url=p["secure_url"], resource_type=p.get("resource_type", "image"), public_id=p["public_id"])
    emb = None
    try: emb = json.dumps(embed(Image.open(io.BytesIO(cloud.fetch_bytes(cloud.embed_url(m))))).tolist())
    except Exception: pass
    tags = p.get("tags") or []
    with db() as c:
        mid = ingest(c, pid, filename=p["public_id"], cloud_id=cid, public_id=p["public_id"], secure_url=p["secure_url"], resource_type=m["resource_type"],
                     fmt=p.get("format"), version=str(p.get("version", "")), tags=json.dumps(tags), phash=p.get("phash"),
                     quality=json.dumps(p.get("quality_analysis")), colors=json.dumps(p.get("colors")), captured_at=when, lat=lat, lng=lng,
                     summary=f"Cloudinary {m['resource_type']}; tags: {', '.join(tags) or 'none'}; phash {p.get('phash')}", embedding=emb)
    if ai.configured():
        try:
            a = ai.analyze(cloud.vlm_url(m))
            with db() as c: c.execute("UPDATE media SET summary=?,stage=?,stage_source='AI_ESTIMATE',analysis=? WHERE id=? AND stage_source='UNKNOWN'",
                                      (a.summary, a.project_stage, a.model_dump_json(), mid))
        except Exception as e:
            with db() as c: c.execute("UPDATE media SET status='AI_FAILED' WHERE id=?", (mid,)); audit(c, "ai_failed", mid, None, str(e)[:200])
    return mid, True

@app.post("/api/projects/{pid}/uploads/sign")
def sign(pid: str):
    with db() as c: need_project(c, pid)
    return cloud.sign_upload(pid) if cloud.configured() else {"configured": False}

@app.post("/api/projects/{pid}/uploads/complete", status_code=201)
def complete(pid: str, p: dict):   # browser reports Cloudinary's upload response (webhook fallback for localhost dev)
    if not p.get("secure_url") or not p.get("public_id"): raise HTTPException(400, "not a Cloudinary upload response")
    mid, new = ingest_cloudinary(pid, p); return {"id": mid, "created": new}

@app.post("/api/webhooks/cloudinary")
async def webhook(request: Request):
    body = await request.body()
    if not cloud.configured(): raise HTTPException(503, "Cloudinary not configured")
    if not cloud.verify_webhook(body, request.headers.get("x-cld-timestamp", ""), request.headers.get("x-cld-signature", "")):
        raise HTTPException(401, "invalid signature")
    p = json.loads(body)
    if p.get("notification_type") != "upload": return {"accepted": True, "skipped": True}
    pid = cloud.project_of(p)
    with db() as c:
        if not pid or not c.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone(): return {"accepted": False, "reason": "unknown project"}
    mid, new = ingest_cloudinary(pid, p); return {"accepted": True, "media_id": mid, "created": new}

@app.post("/api/projects/{pid}/media", status_code=201)
async def upload(pid: str, file: UploadFile = File(...)):
    """Local fallback when Cloudinary is not configured."""
    import hashlib
    raw = await file.read()
    try: img = Image.open(io.BytesIO(raw)); img.load()
    except Exception: raise HTTPException(400, "not a valid image")
    when, lat, lng = read_exif(img); mid0 = uid(); path = data_dir() / "uploads" / f"{mid0}.img"; path.write_bytes(raw)
    with db() as c:
        mid = ingest(c, pid, filename=file.filename, path=str(path), sha256=hashlib.sha256(raw).hexdigest(), captured_at=when, lat=lat, lng=lng,
                     summary=f"{img.width}x{img.height} local image (no Cloudinary)", embedding=json.dumps(embed(img).tolist()))
    return {"id": mid, "captured_at": when, "captured_at_source": "EXIF" if when else "UNKNOWN"}

MEDIA_COLS = "id,filename,captured_at,captured_at_source,lat,lng,location_source,stage,stage_source,summary,status,public_id,secure_url,resource_type,fmt,version,tags,phash,analysis"
def out(r):
    d = dict(r); d["tags"] = json.loads(d["tags"] or "[]"); d["analysis"] = json.loads(d["analysis"]) if d.get("analysis") else None
    d["thumb_url"] = cloud.thumb(d) if d["secure_url"] else f"/api/media/{d['id']}/file"
    d["safe_thumb_url"] = cloud.thumb(d, blur=True) if d["secure_url"] else d["thumb_url"]
    return d

@app.get("/api/projects/{pid}/media")
def media(pid: str):
    with db() as c:
        need_project(c, pid); return [out(r) for r in c.execute(f"SELECT {MEDIA_COLS} FROM media WHERE project_id=?", (pid,))]

@app.get("/api/media/{mid}/file")
def media_file(mid: str):
    with db() as c: r = c.execute("SELECT path FROM media WHERE id=?", (mid,)).fetchone()
    if not r: raise HTTPException(404)
    return FileResponse(r["path"], media_type="image/jpeg")

@app.get("/api/projects/{pid}/timeline")
def timeline(pid: str):
    with db() as c:
        need_project(c, pid); rows = [out(r) for r in c.execute(f"SELECT {MEDIA_COLS} FROM media WHERE project_id=?", (pid,))]
    dated = sorted([r for r in rows if r["captured_at"]], key=lambda r: r["captured_at"])
    return {"dated": dated, "undated": [r for r in rows if not r["captured_at"]],
            "note": "Undated media are observed but not placed on the time axis."}

@app.get("/api/projects/{pid}/pairs")
def pairs(pid: str):
    with db() as c: return [dict(r) for r in c.execute("SELECT * FROM pairs WHERE project_id=?", (pid,))]

@app.get("/api/projects/{pid}/relations")
def relations(pid: str):
    with db() as c:
        return [dict(r) for r in c.execute("SELECT r.* FROM relations r JOIN media m ON m.id=r.a WHERE m.project_id=?", (pid,))]

@app.post("/api/pairs/{pair_id}/verify")
def verify_pair(pair_id: str):
    """AI checks same-site + visible changes. Result is advisory: a human still confirms via /review."""
    if not ai.configured(): raise HTTPException(503, "AI_API_KEY not set")
    with db() as c:
        p = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
        if not p: raise HTTPException(404)
        b, a = (dict(c.execute("SELECT * FROM media WHERE id=?", (x,)).fetchone()) for x in (p["before_id"], p["after_id"]))
    if not (b["secure_url"] and a["secure_url"]): raise HTTPException(400, "pair needs Cloudinary assets")
    r = ai.compare(cloud.vlm_url(b), cloud.vlm_url(a))
    with db() as c:
        c.execute("UPDATE pairs SET changes=?,status=? WHERE id=?", (r.model_dump_json(), "needs_review", pair_id)); audit(c, "ai_compare", pair_id, None, r.summary)
    return r.model_dump()

def grounded(pid, task):
    """LLM answer restricted to project evidence; rejected if it cites anything outside that evidence."""
    t = timeline(pid); ms = t["dated"] + t["undated"]; ids = {m["id"] for m in ms}
    ev = "\n".join(f"[MEDIA:{m['id']}] time={m['captured_at'] or 'UNKNOWN'}({m['captured_at_source']}) stage={m['stage']}({m['stage_source']}) {m['summary']}" for m in ms)
    ev += "\n" + "\n".join(f"PAIR before={p['before_id']} after={p['after_id']} status={p['status']} changes={p['changes']}" for p in pairs(pid))
    try: r = ai.answer(task, ev)
    except Exception: return None
    return (r.answer, r.citations) if r.citations and set(r.citations) <= ids else None

@app.post("/api/pairs/{pair_id}/review")
def review(pair_id: str, r: ReviewIn):
    if r.status not in ("verified", "rejected", "needs_review"): raise HTTPException(400, "bad status")
    with db() as c:
        old = c.execute("SELECT status FROM pairs WHERE id=?", (pair_id,)).fetchone()
        if not old: raise HTTPException(404)
        c.execute("UPDATE pairs SET status=? WHERE id=?", (r.status, pair_id)); audit(c, "pair_review", pair_id, old["status"], r.status)
    return {"ok": True}

@app.post("/api/media/{mid}/phase")
def set_phase(mid: str, p: PhaseIn):
    with db() as c:
        old = c.execute("SELECT stage FROM media WHERE id=?", (mid,)).fetchone()
        if not old: raise HTTPException(404)
        c.execute("UPDATE media SET stage=?,stage_source='USER' WHERE id=?", (p.phase, mid)); audit(c, "phase", mid, old["stage"], p.phase, p.reason)
    return {"ok": True}

@app.post("/api/media/{mid}/capture")
def set_capture(mid: str, p: CaptureIn):
    try: dt.datetime.fromisoformat(p.captured_at)
    except ValueError: raise HTTPException(400, "captured_at must be ISO-8601")
    with db() as c:
        old = c.execute("SELECT captured_at FROM media WHERE id=?", (mid,)).fetchone()
        if not old: raise HTTPException(404)
        c.execute("UPDATE media SET captured_at=?,captured_at_source='USER' WHERE id=?", (p.captured_at, mid)); audit(c, "capture", mid, old["captured_at"], p.captured_at, p.reason)
    return {"ok": True}

@app.get("/api/projects/{pid}/search")
def search(pid: str, q: str):
    toks = [t for t in q.lower().split() if t]
    with db() as c:
        need_project(c, pid); rows = [out(r) for r in c.execute(f"SELECT {MEDIA_COLS} FROM media WHERE project_id=?", (pid,))]
    def s(r): h = f"{r['filename']} {r['stage']} {r['summary']} {' '.join(r['tags'])} {json.dumps(r['analysis'] or {})}".lower(); return sum(t in h for t in toks)
    return [r for r in sorted(rows, key=s, reverse=True) if s(r) > 0]

@app.post("/api/projects/{pid}/ask")
def ask(pid: str, a: AskIn):
    if ai.configured():
        g = grounded(pid, a.question)
        if g: return {"answer": g[0], "citations": g[1], "source": "ai"}
    t = timeline(pid)["dated"]
    if len(t) < 2: return {"answer": "Insufficient dated evidence: at least two media with verified capture time are needed.", "citations": []}
    f, l = t[0], t[-1]; cites = [f["id"], l["id"]]
    return {"answer": f"Earliest dated media [MEDIA:{f['id']}] ({f['captured_at']}, source {f['captured_at_source']}) and latest [MEDIA:{l['id']}] "
                      f"({l['captured_at']}, source {l['captured_at_source']}). Visual change is not assessed without a VLM; review the before/after pairs.",
            "citations": cites}

class AssignIn(BaseModel): project_id: str; reason: str | None = None

@app.get("/api/media/{mid}/suggest-project")
def suggest(mid: str):
    """Which OTHER project does this media look like it belongs to? Suggestion only."""
    with db() as c:
        m = c.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
        if not m: raise HTTPException(404)
        if not m["embedding"]: return []
        best = {}
        for o in c.execute("SELECT * FROM media WHERE project_id!=? AND embedding IS NOT NULL", (m["project_id"],)):
            _, sig, sc = pscore(dict(m), dict(o))
            if sc > best.get(o["project_id"], (0,))[0]: best[o["project_id"]] = (sc, {k: v[0] for k, v in sig.items()})
        names = {r["id"]: r["name"] for r in c.execute("SELECT * FROM projects")}
    return sorted([{"project_id": p, "name": names[p], "score": sc, "signals": g, "note": "suggestion only"} for p, (sc, g) in best.items()], key=lambda x: -x["score"])[:3]

@app.post("/api/media/{mid}/assign")
def assign(mid: str, a: AssignIn):
    with db() as c:
        old = c.execute("SELECT project_id FROM media WHERE id=?", (mid,)).fetchone()
        if not old: raise HTTPException(404)
        need_project(c, a.project_id); c.execute("UPDATE media SET project_id=? WHERE id=?", (a.project_id, mid)); audit(c, "assign_project", mid, old["project_id"], a.project_id, a.reason)
    return {"ok": True}

@app.get("/api/projects/{pid}/derived")
def derived(pid: str):
    """Every transformed URL we handed out, tied to its original asset + version."""
    with db() as c: return [dict(r) for r in c.execute("SELECT d.* FROM derived d JOIN media m ON m.id=d.media_id WHERE m.project_id=?", (pid,))]

@app.get("/api/projects/{pid}/campaign")
def campaign(pid: str):
    """Campaign-ready cards from VERIFIED pairs only; captions state dates/sources, never impact claims."""
    with db() as c:
        need_project(c, pid); cards = []
        for p in c.execute("SELECT * FROM pairs WHERE project_id=? AND status='verified'", (pid,)):
            b, a = (c.execute("SELECT * FROM media WHERE id=?", (x,)).fetchone() for x in (p["before_id"], p["after_id"]))
            if not (b["secure_url"] and a["secure_url"]): continue
            for x in (b, a): c.execute("INSERT INTO derived VALUES(?,?,?,?,?,?,?)", (uid(), x["id"], x["public_id"], x["version"], "before_after_composite", cloud.before_after_url(dict(b), dict(a)), now()))
            cards.append({"pair_id": p["id"], "image_url": cloud.before_after_url(dict(b), dict(a)),
                          "caption": f"Before ({b['captured_at'][:10]}) and after ({a['captured_at'][:10]}), same site, human-verified.",
                          "alt": "Side-by-side comparison of the same site at two dates", "citations": [b["id"], a["id"]],
                          "source_public_ids": [b["public_id"], a["public_id"]], "transformation": "cloudinary layer composite of originals"})
    return cards

@app.post("/api/projects/{pid}/report")
def report(pid: str):
    with db() as c:
        need_project(c, pid); name = c.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()["name"]
    tl = timeline(pid); ps = pairs(pid); n = len(tl["dated"]) + len(tl["undated"])
    g = grounded(pid, "Write a short factual summary of the visible project progress.") if ai.configured() else None
    md = [f"# {name} - Evidence Report"] + ([f"## Summary\n{g[0]}"] if g else []) + [f"Media: {n}; dated: {len(tl['dated'])}; undated: {len(tl['undated'])}.", "## Timeline"]
    md += [f"- {r['captured_at']} ({r['captured_at_source']}) [MEDIA:{r['id']}]" for r in tl["dated"]] or ["- No dated media."]
    md += ["## Before/after"] + [f"- [MEDIA:{p['before_id']}] -> [MEDIA:{p['after_id']}] ({p['status']}, score {p['confidence']:.2f})" for p in ps if p["status"] != "rejected"]
    md += ["## Source assets (Cloudinary)"] + [f"- [MEDIA:{r['id']}] {r['public_id']} v{r['version']} ![]({r['safe_thumb_url']})" for r in tl["dated"] + tl["undated"] if r["public_id"]]
    md += ["## Limitations", "- Times/locations come only from EXIF or user input; unknown stays unknown.",
           "- No causal or social-impact claims are made from images."]
    return {"markdown": "\n".join(md)}

@app.get("/api/projects/{pid}/report.pdf")
def report_pdf(pid: str):
    import re
    from xml.sax.saxutils import escape
    from reportlab.platypus import SimpleDocTemplate, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet
    st = getSampleStyleSheet(); story = []; buf = io.BytesIO()
    for ln in report(pid)["markdown"].splitlines():
        ln = re.sub(r"!\[\]\(.*?\)", "", ln)
        if ln.startswith("#"): story.append(Paragraph(escape(ln.lstrip("# ")), st["Heading2"]))
        elif ln.strip(): story.append(Paragraph(escape(ln), st["BodyText"]))
    SimpleDocTemplate(buf).build(story); return Response(buf.getvalue(), media_type="application/pdf")

app.mount("/", StaticFiles(directory=str(Path(__file__).parent.parent / "static"), html=True), name="static")
