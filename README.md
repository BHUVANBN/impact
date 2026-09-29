# Impact Evidence Platform

An AI-powered field-media intelligence and provenance platform that reconstructs project timelines, identifies before/after evidence pairs, and generates verifiable, evidence-backed reports from unstructured photos and videos.

---

## Key Features

- **Provenance-First Metadata**: Distinguishes verified camera metadata (`EXIF`) from `UNKNOWN` metadata. Never invents capture dates or GPS locations.
- **Cloudinary Integration**: Signed direct browser uploads, HMAC-verified webhooks, transformation URLs (face-blurring, responsive thumbnails, video keyframe extraction), and fallback direct ingestion.
- **Multimodal AI Vision & Structured Output**: Supports both **Google Gemini** (`gemini-2.5-flash`, `gemini-1.5-flash`) and **Anthropic Claude** APIs with Pydantic schema validation (`Analysis`, `Change`, `Answer`) and prompt injection defenses.
- **Relationship & Pair Discovery**: Spatial (Haversine distance) and visual embedding similarity algorithms to discover duplicate assets and before/after project pairs.
- **Human-in-the-Loop Review**: Manual review workflow for before/after pairs (`needs_review`, `verified`, `rejected`), metadata overrides, and audit trail logging.
- **Traceable Q&A & Reports**: Evidence-backed Q&A and automated Markdown reports that cite source media IDs (`[MEDIA:id]`) and detail data limitations.
- **Modern UI Theme Engine**: Includes 21 themes (`tokyo-night`, `catppuccin`, `nord`, `gruvbox`, `rose-pine`, `solitude`, `vantablack`, etc.) with instant keyboard shortcut switching (`T`).

---

## Quick Start

### 1. Prerequisites
- Python 3.10+
- Virtual environment (`.venv`)

### 2. Installation
```bash
# Clone the repository & set up environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Environment Configuration
Copy `.env.example` to `.env` and fill in your keys:
```bash
cp .env.example .env
```

Example `.env` configuration:
```ini
# Cloudinary Configuration
CLOUDINARY_CLOUD_NAME=your_cloud_name
CLOUDINARY_API_KEY=your_api_key
CLOUDINARY_API_SECRET=your_api_secret

# AI Vision & LLM Configuration (Gemini or Anthropic)
AI_API_KEY=your_gemini_or_anthropic_api_key
AI_VISION_MODEL=gemini-2.5-flash
```

### 4. Running the Application
```bash
# Run FastAPI server with Uvicorn
PYTHONPATH=. uvicorn app.main:app --reload --port 8000
```
Open **[http://localhost:8000](http://localhost:8000)** in your browser.

### 5. Running Tests
```bash
# Execute unit & integration test suite
PYTHONPATH=. pytest
```

---

## API Overview

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `/api/projects` | `GET` / `POST` | List all projects or create a new project |
| `/api/projects/{id}/uploads/sign` | `POST` | Generate signed upload parameters for Cloudinary |
| `/api/projects/{id}/media` | `GET` / `POST` | Retrieve media assets or upload via fallback multipart form |
| `/api/webhooks/cloudinary` | `POST` | Cloudinary notification webhook with HMAC verification |
| `/api/projects/{id}/timeline` | `GET` | Retrieve chronological project timeline (dated vs. undated) |
| `/api/projects/{id}/pairs` | `GET` | Get discovered before/after evidence pairs |
| `/api/pairs/{pair_id}/review` | `POST` | Confirm (`verified`) or reject (`rejected`) pair candidates |
| `/api/projects/{id}/search` | `GET` | Search project media, tags, and metadata |
| `/api/projects/{id}/ask` | `POST` | Ask evidence-backed questions regarding project progression |
| `/api/projects/{id}/report` | `POST` | Generate a comprehensive, traceable project report |

---

## Architecture & Technology Stack

- **Backend**: Python 3, FastAPI, SQLite, Pydantic, Pillow, NumPy, HTTPX
- **Media Engine**: Cloudinary SDK & REST API (Transformations, Webhooks, Signed Uploads)
- **AI / VLM Layer**: Google Gemini API & Anthropic Messages API
- **Frontend**: Vanilla JS, HTML5, Custom CSS with 21 Omarchy-inspired color themes
