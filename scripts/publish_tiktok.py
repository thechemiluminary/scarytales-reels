#!/usr/bin/env python3
"""
ScaryTales TikTok Publisher — GitHub Actions version.

Reads manifest.json from the scarytales-reels repo, finds reels not yet posted
to TikTok (posted_tt is falsy), downloads each MP4 from GitHub Releases,
uploads to Cloudinary (CDN), and posts via the Lime Social API.

After each successful post, updates manifest.json with posted_tt + tt_publish_id
and pushes back to GitHub immediately (no double-posting on partial failures).

Environment variables:
    LIMESOCIAL_API_KEY        — Lime Social API key
    LIMESOCIAL_TT_USERNAME    — TikTok username connected to Lime Social
    SCARYTALES_PAT            — GitHub PAT with contents:write on scarytales-reels
    CLOUDINARY_CLOUD_NAME     — Cloudinary cloud name
    CLOUDINARY_UPLOAD_PRESET  — Cloudinary unsigned upload preset name
"""

import base64
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
OWNER = "thechemiluminary"
REPO = "scarytales-reels"
API = "https://api.github.com"
LIME_API = "https://api.limesocial.io/v1/post"
POST_DELAY_SEC = 15  # rate-limit guard between consecutive posts
MAX_POST = 10  # safety cap per run

# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------
LIME_KEY = os.environ.get("LIMESOCIAL_API_KEY", "").strip()
LIME_USER = os.environ.get("LIMESOCIAL_TT_USERNAME", "").strip()
PAT = os.environ.get("SCARYTALES_PAT", "").strip()
CF_CLOUD = os.environ.get("CLOUDINARY_CLOUD_NAME", "").strip()
CF_PRESET = os.environ.get("CLOUDINARY_UPLOAD_PRESET", "").strip()

if not LIME_KEY:
    sys.exit("LIMESOCIAL_API_KEY is not set.")
if not LIME_USER:
    sys.exit("LIMESOCIAL_TT_USERNAME is not set.")
if not PAT:
    sys.exit("SCARYTALES_PAT is not set.")
if not CF_CLOUD:
    sys.exit("CLOUDINARY_CLOUD_NAME is not set.")
if not CF_PRESET:
    sys.exit("CLOUDINARY_UPLOAD_PRESET is not set.")

GH_HEADERS = {
    "Authorization": f"Bearer {PAT}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "scarytales-reels",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def gh_get(path: str):
    r = requests.get(f"{API}/repos/{OWNER}/{REPO}/{path}", headers=GH_HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()


def load_manifest() -> tuple[dict, str]:
    """Return (manifest dict, current SHA)."""
    data = gh_get("contents/manifest.json")
    sha = data["sha"]
    manifest = json.loads(base64.b64decode(data["content"]))
    return manifest, sha


def push_manifest(manifest: dict, sha: str, message: str) -> str:
    """Push manifest and return new SHA."""
    content = base64.b64encode(json.dumps(manifest, indent=2).encode()).decode()
    payload = {"message": message, "content": content, "sha": sha, "branch": "main"}
    r = requests.put(
        f"{API}/repos/{OWNER}/{REPO}/contents/manifest.json",
        headers=GH_HEADERS,
        json=payload,
        timeout=30,
    )
    if r.status_code == 409:
        # SHA conflict — reload and retry once
        _, sha = load_manifest()
        payload["sha"] = sha
        r = requests.put(
            f"{API}/repos/{OWNER}/{REPO}/contents/manifest.json",
            headers=GH_HEADERS,
            json=payload,
            timeout=30,
        )
    r.raise_for_status()
    return r.json()["content"]["sha"]


# ---------------------------------------------------------------------------
# Cloudinary upload
# ---------------------------------------------------------------------------

def download_video(url: str, dest: str):
    """Stream-download a video from URL to local path."""
    with requests.get(url, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)


def upload_to_cloudinary(local_path: str, slug: str) -> str:
    """Upload video to Cloudinary via unsigned upload (REST API), return CDN URL."""
    upload_url = f"https://api.cloudinary.com/v1_1/{CF_CLOUD}/video/upload"
    with open(local_path, "rb") as f:
        files = {"file": (os.path.basename(local_path), f, "video/mp4")}
        data = {
            "upload_preset": CF_PRESET,
            "public_id": slug,
        }
        r = requests.post(upload_url, files=files, data=data, timeout=600)
    if r.status_code >= 400:
        raise RuntimeError(f"Cloudinary HTTP {r.status_code}: {r.text[:500]}")
    return r.json()["secure_url"]


# ---------------------------------------------------------------------------
# Lime Social post
# ---------------------------------------------------------------------------

def post_to_tiktok(video_url: str, caption: str) -> dict:
    """
    Post a video to TikTok via Lime Social.
    Returns the JSON response on success, raises on failure.
    """
    payload = {
        "accounts": [{"platform": "tiktok", "username": LIME_USER, "post_mode": "MEDIA_UPLOAD"}],
        "title": caption,
        "mediaUrl": video_url,
    }
    r = requests.post(
        LIME_API,
        headers={
            "Authorization": f"Bearer {LIME_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Lime Social HTTP {r.status_code}: {r.text[:500]}")
    return r.json()


# ---------------------------------------------------------------------------
# Caption builder
# ---------------------------------------------------------------------------

def build_caption(meta: dict, slug: str) -> str:
    """Build a TikTok caption from manifest metadata. Max 2,200 chars."""
    title = (meta.get("title") or "").strip() or slug.replace("-", " ").title()
    desc = (meta.get("description") or "").strip()
    caption = f"{title}\n\n{desc}" if desc else title
    return caption[:2200]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] ScaryTales TikTok Publisher")
    print(f"  Lime Social user: {LIME_USER}")
    print()

    # 1. Load manifest
    manifest, sha = load_manifest()
    print(f"manifest.json loaded: {len(manifest)} entries")

    # 2. Find unposted reels, sorted chronologically by date
    pending = []
    for slug, meta in manifest.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("posted_tt"):
            continue
        pending.append((slug, meta))

    # Sort by date (oldest first) then by slug
    pending.sort(key=lambda x: (x[1].get("date", ""), x[0]))

    if not pending:
        print("Nothing to post — all reels already have posted_tt set.")
        return

    # Cap
    to_post = pending[:MAX_POST]
    print(f"Found {len(pending)} unposted reel(s), will post {len(to_post)} this run.\n")

    # 3. Post each reel
    posted = []
    failed = []

    for i, (slug, meta) in enumerate(to_post, 1):
        video_url = meta.get("file", "")
        if not video_url:
            print(f"[{i}/{len(to_post)}] {slug} — SKIP (no file URL in manifest)")
            failed.append(slug)
            continue

        caption = build_caption(meta, slug)
        print(f"[{i}/{len(to_post)}] {slug}")
        print(f"  URL: {video_url[:80]}...")
        print(f"  Caption: {caption[:80]}...")

        try:
            with tempfile.TemporaryDirectory() as tmp:
                local_path = os.path.join(tmp, f"{slug}.mp4")
                print(f"  Downloading from GitHub...")
                download_video(video_url, local_path)
                size_mb = os.path.getsize(local_path) / (1024 * 1024)
                print(f"  Downloaded {size_mb:.1f} MB")

                print(f"  Uploading to Cloudinary...")
                cdn_url = upload_to_cloudinary(local_path, slug)
                print(f"  CDN URL: {cdn_url}")

                resp = post_to_tiktok(cdn_url, caption)
                print(f"  OK — response: {json.dumps(resp)[:200]}")
                post_id = resp.get("id") or resp.get("post_id") or resp.get("data", {}).get("id", "unknown")

            # Mark posted immediately
            manifest[slug]["posted_tt"] = True
            manifest[slug]["tt_publish_id"] = post_id
            manifest[slug]["tt_posted_at"] = datetime.now(timezone.utc).isoformat()
            sha = push_manifest(manifest, sha, f"Mark {slug} posted to TikTok")
            print(f"  Manifest updated (posted_tt=true)")

            posted.append(slug)
        except Exception as e:
            print(f"  FAILED: {e}")
            failed.append(slug)

        # Rate limit between posts
        if i < len(to_post):
            print(f"  Waiting {POST_DELAY_SEC}s before next post...")
            time.sleep(POST_DELAY_SEC)

        print()

    # 4. Summary
    print("=" * 50)
    print(f"Posted: {len(posted)} / {len(to_post)}")
    if posted:
        print(f"  {', '.join(posted)}")
    if failed:
        print(f"Failed: {len(failed)}")
        print(f"  {', '.join(failed)}")
    print("Done.")


if __name__ == "__main__":
    main()
