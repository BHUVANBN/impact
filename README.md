# Impact Evidence Platform (Cloudinary-based)
Run: `pip install -r requirements.txt`, export the CLOUDINARY_* vars from .env.example, `uvicorn app.main:app`, open http://localhost:8000
Test: `pytest` (Cloudinary is simulated: signing and webhook verification are real, network calls are stubbed)
Without Cloudinary keys the app falls back to local upload so you can still try the flow.

Cloudinary use: signed direct browser upload (SDK signing) requesting image_metadata, phash, colors, quality_analysis; webhook with
signature+timestamp check and idempotency; /uploads/complete as webhook fallback on localhost; EXIF time/GPS and tags from Cloudinary
with provenance; thumbnails, face-blurred gallery images, video keyframes and before/after composites via transformation URLs;
campaign cards from human-verified pairs only; reports list source public_id + version.
Unverified against a live account: transformation URLs, upload params and add-on availability depend on your Cloudinary plan.
Not included: CLIP/VLM analysis (search = keywords + Cloudinary tags), Postgres/Redis, auth.
