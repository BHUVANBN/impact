import io, pytest
from PIL import Image
from fastapi.testclient import TestClient

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    for k in ("CLOUD_NAME", "API_KEY", "API_SECRET", "NOTIFICATION_URL", "AUTO_TAGGING"): monkeypatch.delenv("CLOUDINARY_" + k, raising=False)
    from app.main import app
    return TestClient(app)

def img(color, when=None, gps=None, block=False):
    im = Image.new("RGB", (64, 64), color)
    if block: im.paste((120, 120, 130), (8, 20, 48, 64))
    for x in range(0, 64, 8): im.putpixel((x, x), (255 - color[0], 0, 0))
    ex = Image.Exif()
    if when: ex.get_ifd(0x8769)[36867] = when
    if gps: ex.get_ifd(0x8825).update({1: "N", 2: (12.0, 58.0, 17.76), 3: "E", 4: (77.0, 35.0, 40.56)})
    b = io.BytesIO(); im.save(b, "JPEG", exif=ex); return ("a.jpg", b.getvalue(), "image/jpeg")

def up(c, pid, f): return c.post(f"/api/projects/{pid}/media", files={"file": f})

def test_full_flow(client):
    pid = client.post("/api/projects", json={"name": "School"}).json()["id"]
    a = up(client, pid, img((10, 200, 10), "2024:03:15 08:30:00", True)).json()
    assert a["captured_at"].startswith("2024-03-15") and a["captured_at_source"] == "EXIF"
    up(client, pid, img((10, 200, 10), "2024:08:12 16:20:00", True, block=True))
    u = up(client, pid, img((200, 10, 10))).json()
    assert u["captured_at"] is None and u["captured_at_source"] == "UNKNOWN"   # never invented
    m = client.get(f"/api/projects/{pid}/media").json()
    assert sum(1 for x in m if x["location_source"] == "EXIF") == 2
    tl = client.get(f"/api/projects/{pid}/timeline").json()
    assert len(tl["dated"]) == 2 and len(tl["undated"]) == 1
    pairs = client.get(f"/api/projects/{pid}/pairs").json()
    assert len(pairs) == 1 and pairs[0]["status"] == "needs_review"
    assert client.post(f"/api/pairs/{pairs[0]['id']}/review", json={"status": "verified"}).status_code == 200
    ask = client.post(f"/api/projects/{pid}/ask", json={"question": "what changed?"}).json()
    assert len(ask["citations"]) == 2 and all(c in ask["answer"] for c in ask["citations"])
    assert client.post(f"/api/media/{u['id']}/capture", json={"captured_at": "2024-05-01T10:00:00"}).status_code == 200
    assert len(client.get(f"/api/projects/{pid}/timeline").json()["dated"]) == 3
    assert "Limitations" in client.post(f"/api/projects/{pid}/report").json()["markdown"]
    assert client.get(f"/api/media/{u['id']}/file").status_code == 200

def test_rejects_non_image_and_missing_project(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/media", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400
    assert up(client, "nope", img((1, 2, 3))).status_code == 404

def test_ask_insufficient(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/ask", json={"question": "?"}).json()["citations"] == []

# ---------- Cloudinary (simulated: signing/verification are real, network is stubbed) ----------
import json, time, hashlib, cloudinary.utils
from PIL import Image as _I

@pytest.fixture
def cld(client, monkeypatch):
    monkeypatch.setenv("CLOUDINARY_CLOUD_NAME", "demo"); monkeypatch.setenv("CLOUDINARY_API_KEY", "k"); monkeypatch.setenv("CLOUDINARY_API_SECRET", "sekret")
    from app import cloud
    def fake(url):
        im = _I.new("RGB", (16, 16), (10, 200, 10))
        if "after" in url: im.paste((120, 120, 130), (2, 5, 12, 16))
        b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()
    monkeypatch.setattr(cloud, "fetch_bytes", fake); return client

def payload(pid, name, when=None, lat=True):
    md = {"DateTimeOriginal": when} if when else {}
    if lat: md.update({"GPSLatitude": "12 deg 58' 17.76\" N", "GPSLongitude": "77 deg 35' 40.56\" E"})
    return {"notification_type": "upload", "asset_id": "a_" + name, "public_id": f"impact/{pid}/{name}", "version": 1,
            "resource_type": "image", "format": "jpg", "tags": ["building", "site"], "phash": "abc123", "image_metadata": md,
            "secure_url": f"https://res.cloudinary.com/demo/image/upload/v1/impact/{pid}/{name}.jpg", "created_at": "2030-01-01T00:00:00Z"}

def hook(c, p, sig_ok=True, ts=None):
    body = json.dumps(p).encode(); ts = str(ts or int(time.time()))
    sig = hashlib.sha1(body + ts.encode() + b"sekret").hexdigest() if sig_ok else "bad"
    return c.post("/api/webhooks/cloudinary", content=body, headers={"x-cld-timestamp": ts, "x-cld-signature": sig})

def test_sign_matches_sdk_and_unconfigured(client, cld):
    pid = cld.post("/api/projects", json={"name": "x"}).json()["id"]
    s = cld.post(f"/api/projects/{pid}/uploads/sign").json()
    assert s["configured"] and s["signature"] == cloudinary.utils.api_sign_request(s["params"], "sekret")
    assert s["params"]["folder"] == f"impact/{pid}" and s["params"]["image_metadata"] == "true"

def test_unconfigured_sign(client):
    pid = client.post("/api/projects", json={"name": "x"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/uploads/sign").json() == {"configured": False}

def test_webhook_security_and_idempotency(cld):
    pid = cld.post("/api/projects", json={"name": "x"}).json()["id"]; p = payload(pid, "one", "2024:03:15 08:30:00")
    assert hook(cld, p, sig_ok=False).status_code == 401
    assert hook(cld, p, ts=int(time.time()) - 99999).status_code == 401
    assert hook(cld, p).json()["created"] is True and hook(cld, p).json()["created"] is False
    m = cld.get(f"/api/projects/{pid}/media").json()
    assert len(m) == 1 and m[0]["captured_at"].startswith("2024-03-15") and m[0]["tags"] == ["building", "site"]
    assert abs(m[0]["lat"] - 12.9716) < 1e-3 and "c_fill" in m[0]["thumb_url"] and "e_blur_faces" in m[0]["safe_thumb_url"]

def test_upload_time_never_capture_time(cld):
    pid = cld.post("/api/projects", json={"name": "x"}).json()["id"]
    hook(cld, payload(pid, "nodate")); m = cld.get(f"/api/projects/{pid}/media").json()[0]
    assert m["captured_at"] is None and m["captured_at_source"] == "UNKNOWN"

def test_cloudinary_pipeline_to_campaign(cld):
    pid = cld.post("/api/projects", json={"name": "School"}).json()["id"]
    hook(cld, payload(pid, "before", "2024:03:15 08:30:00")); hook(cld, payload(pid, "after", "2024:08:12 16:20:00"))
    prs = cld.get(f"/api/projects/{pid}/pairs").json(); assert len(prs) == 1
    assert cld.get(f"/api/projects/{pid}/campaign").json() == []          # unverified pairs are never published
    cld.post(f"/api/pairs/{prs[0]['id']}/review", json={"status": "verified"})
    card = cld.get(f"/api/projects/{pid}/campaign").json()[0]
    assert "l_impact:" in card["image_url"] and "fl_layer_apply" in card["image_url"] and len(card["citations"]) == 2
    assert len(card["source_public_ids"]) == 2
    assert "impact/" in cld.post(f"/api/projects/{pid}/report").json()["markdown"]
    assert cld.get(f"/api/projects/{pid}/search", params={"q": "building"}).json()

def test_video_keyframe_and_complete_fallback(cld):
    pid = cld.post("/api/projects", json={"name": "x"}).json()["id"]
    p = payload(pid, "clip"); p.update(resource_type="video", format="mp4", secure_url=f"https://res.cloudinary.com/demo/video/upload/v1/impact/{pid}/clip.mp4")
    assert cld.post(f"/api/projects/{pid}/uploads/complete", json=p).status_code == 201
    assert "so_1" in cld.get(f"/api/projects/{pid}/media").json()[0]["thumb_url"] and ".jpg" in cld.get(f"/api/projects/{pid}/media").json()[0]["thumb_url"]
