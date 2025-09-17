import time
import requests
import os
import base64
from urllib.parse import urljoin
from app.config import RUNWAY_API_KEY, RUNWAY_API_URL

# Validate configuration
if not RUNWAY_API_KEY:
    raise ValueError("RUNWAY_API_KEY environment variable is not set.")
if not RUNWAY_API_URL:
    raise ValueError("RUNWAY_API_URL environment variable is not set.")

headers = {"Authorization": f"Bearer {RUNWAY_API_KEY}"}
json_headers = {**headers, "Content-Type": "application/json"}

# Endpoints
IMAGE_TO_VIDEO_ENDPOINT = os.getenv("RUNWAY_IMAGE_TO_VIDEO_ENDPOINT", "/image_to_video")
TASKS_ENDPOINT = os.getenv("RUNWAY_TASKS_ENDPOINT", "/tasks")

def generate_video(prompt: str, image_path: str, output_path: str, video_id: int = None):
    """
    Send prompt + image to RunwayML API, poll until video is ready,
    then save locally. Updates database status in real-time.
    """
    from app.models.database import SessionLocal, Video
    from datetime import datetime
    
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image file not found: {image_path}")
    if not prompt.strip():
        raise ValueError("Prompt cannot be empty")

    # Update status to processing if video_id provided
    if video_id:
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                video.status = "processing"
                video.updated_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    tasks_url = urljoin(RUNWAY_API_URL.rstrip("/") + "/", IMAGE_TO_VIDEO_ENDPOINT.lstrip("/"))
    payload = {
        "task": "image_to_video",
        "inputs": {
            "prompt": prompt,
            "model": "gen-4",
            "image": {"type": "image", "b64": image_b64},
        },
    }

    try:
        response = requests.post(tasks_url, headers=json_headers, json=payload, timeout=120)
        response.raise_for_status()
    except requests.RequestException as e:
        # Update status to failed
        if video_id:
            db = SessionLocal()
            try:
                video = db.query(Video).filter(Video.id == video_id).first()
                if video:
                    video.status = "failed"
                    video.updated_at = datetime.utcnow()
                    db.commit()
            finally:
                db.close()
        raise Exception(f"RunwayML job creation failed: {str(e)}") from e

    job = response.json()
    job_id = job.get("id")
    if not job_id:
        raise Exception(f"Unexpected RunwayML response structure: {job}")

    # Update runway_job_id in database
    if video_id:
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                video.runway_job_id = job_id
                video.updated_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    # Polling with real-time status updates
    status_url = urljoin(RUNWAY_API_URL.rstrip("/") + "/", TASKS_ENDPOINT.lstrip("/") + f"/{job_id}")
    max_attempts, attempts = 60, 0

    while attempts < max_attempts:
        try:
            res = requests.get(status_url, headers=headers, timeout=60)
            res.raise_for_status()
        except requests.RequestException as e:
            # Update status to failed
            if video_id:
                db = SessionLocal()
                try:
                    video = db.query(Video).filter(Video.id == video_id).first()
                    if video:
                        video.status = "failed"
                        video.updated_at = datetime.utcnow()
                        db.commit()
                finally:
                    db.close()
            raise Exception(f"Failed to check task status: {str(e)}") from e

        task_status = res.json()
        status = task_status.get("status", "").upper()

        if status == "SUCCEEDED":
            outputs = task_status.get("outputs", [])
            video_url = outputs[0].get("url") if outputs else None
            if not video_url:
                raise Exception(f"Task completed but no video URL: {task_status}")

            try:
                video_resp = requests.get(video_url, timeout=300)
                video_resp.raise_for_status()
            except requests.RequestException as e:
                raise Exception(f"Failed to download video: {str(e)}") from e

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "wb") as v:
                v.write(video_resp.content)

            # Update status to succeeded
            if video_id:
                db = SessionLocal()
                try:
                    video = db.query(Video).filter(Video.id == video_id).first()
                    if video:
                        video.status = "succeeded"
                        video.video_path = output_path
                        video.video_url = video_url
                        video.updated_at = datetime.utcnow()
                        db.commit()
                finally:
                    db.close()

            return {"video_url": video_url, "file_path": output_path, "runway_job_id": job_id}

        if status == "FAILED":
            # Update status to failed
            if video_id:
                db = SessionLocal()
                try:
                    video = db.query(Video).filter(Video.id == video_id).first()
                    if video:
                        video.status = "failed"
                        video.updated_at = datetime.utcnow()
                        db.commit()
                finally:
                    db.close()
            raise Exception(f"RunwayML video generation failed: {task_status.get('error', task_status)}")

        time.sleep(5)
        attempts += 1

    # Update status to failed on timeout
    if video_id:
        db = SessionLocal()
        try:
            video = db.query(Video).filter(Video.id == video_id).first()
            if video:
                video.status = "failed"
                video.updated_at = datetime.utcnow()
                db.commit()
        finally:
            db.close()

    raise Exception(f"Video generation timed out after {max_attempts * 5} seconds")
