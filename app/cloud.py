"""Cloudinary integration: signed direct upload, webhook verification, metadata parsing, transformation URLs."""
import os, re, hmac, time, hashlib, json
import httpx
import cloudinary.utils

def cfg(k): return os.getenv(f"CLOUDINARY_{k}", "")
def configured(): return all(cfg(k) for k in ("CLOUD_NAME", "API_KEY", "API_SECRET"))

def sign_upload(pid):
    """Params the browser posts straight to Cloudinary. Metadata signals are requested at upload time."""
    p = {"timestamp": int(time.time()), "folder": f"impact/{pid}", "image_metadata": "true", "phash": "true",
         "colors": "true", "quality_analysis": "true", "context": f"project_id={pid}"}
    if cfg("NOTIFICATION_URL"): p["notification_url"] = cfg("NOTIFICATION_URL")
    if cfg("AUTO_TAGGING"): p["auto_tagging"] = cfg("AUTO_TAGGING")      # needs a tagging add-on on your plan
    sig = cloudinary.utils.api_sign_request(p, cfg("API_SECRET"))
    return {"configured": True, "upload_url": f"https://api.cloudinary.com/v1_1/{cfg('CLOUD_NAME')}/auto/upload",
            "api_key": cfg("API_KEY"), "signature": sig, "params": p}

def verify_webhook(body: bytes, ts: str, sig: str, max_age=7200):
    if not (cfg("API_SECRET") and ts and sig): return False
    try: if_old = abs(time.time() - int(ts)) > max_age
    except ValueError: return False
    if if_old: return False
    return any(hmac.compare_digest(h(body + ts.encode() + cfg("API_SECRET").encode()).hexdigest(), sig) for h in (hashlib.sha1, hashlib.sha256))

def project_of(payload):
    m = re.search(r"impact/([0-9a-f]{32})/", (payload.get("asset_folder") or "") + "/" + (payload.get("public_id") or "") + "/")
    return m.group(1) if m else (payload.get("context", {}).get("custom", {}) or {}).get("project_id")

def parse_gps(v, ref=None):
    if v is None: return None
    if isinstance(v, (int, float)): return float(v)
    n = [float(x) for x in re.findall(r"[\d.]+", str(v))]
    if not n: return None
    r = n[0] + (n[1] / 60 if len(n) > 1 else 0) + (n[2] / 3600 if len(n) > 2 else 0)
    return -r if re.search(r"[SW]\b", str(v) + str(ref or "")) else r

def transform(url, t):
    return url.replace("/upload/", f"/upload/{t}/", 1) if url and "/upload/" in url else url

def thumb(m, blur=False):
    t = "c_fill,w_400,h_300,q_auto,f_auto" + (",e_blur_faces:2000" if blur else "")
    if m["resource_type"] == "video":   # keyframe from the video; no ffmpeg needed
        return transform(re.sub(r"\.\w+$", ".jpg", m["secure_url"]), "so_1," + t)
    return transform(m["secure_url"], t)

def embed_url(m):
    u = re.sub(r"\.\w+$", ".png", m["secure_url"]) if m["resource_type"] == "video" else m["secure_url"]
    return transform(u, ("so_1," if m["resource_type"] == "video" else "") + "c_scale,w_16,h_16,f_png")

def before_after_url(b, a):
    """Side-by-side composite built entirely by Cloudinary layer transformations."""
    ov = a["public_id"].replace("/", ":")
    return transform(b["secure_url"], f"c_fill,w_600,h_400/c_pad,w_1200,h_400,g_west,b_white/l_{ov},c_fill,w_600,h_400/fl_layer_apply,g_east")

def fetch_bytes(url):
    r = httpx.get(url, timeout=20, follow_redirects=True); r.raise_for_status(); return r.content

def vlm_url(m):
    v = m["resource_type"] == "video"
    u = re.sub(r"\.\w+$", ".jpg", m["secure_url"]) if v else m["secure_url"]
    return transform(u, ("so_1," if v else "") + "c_limit,w_1024,q_auto,f_jpg")
