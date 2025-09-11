import os
import base64
import shutil
import tempfile
from datetime import datetime
import requests
from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from PIL import Image
from dotenv import load_dotenv
from typing import List, Optional

from runwayml import RunwayML

from app.models.database import SessionLocal, UploadedImage, Video, Feedback
from app.services.prompt_generator import (
    generate_cinematic_prompt_from_image,
    improve_prompt_with_feedback,
)
PACKAGE_LIMITS = {
    "Starter": (5, 10),
    "Professional": (11, 20),
    "Premium": (21, 30)
}

# ----------------------- SETUP -----------------------
router = APIRouter()
load_dotenv()

# Mock mode via env: RUNWAY_MOCK=true|1|yes
USE_MOCK_RUNWAY = str(os.getenv("RUNWAY_MOCK", "true")).lower() in {"1", "true", "yes"}

# API key for SDK (supports either env var name)
RUNWAY_API_KEY = os.getenv("RUNWAYML_API_SECRET") or os.getenv("RUNWAY_API_KEY")
RUNWAY_MODEL = os.getenv("RUNWAY_MODEL", "gen4_turbo")

VIDEOS_DIR = "videos"
IMAGES_DIR = "uploaded_images"
os.makedirs(VIDEOS_DIR, exist_ok=True)
os.makedirs(IMAGES_DIR, exist_ok=True)

# Initialize SDK client only if not in mock mode
client: Optional[RunwayML] = None
if not USE_MOCK_RUNWAY:
    if not RUNWAY_API_KEY:
        raise RuntimeError("Runway API key missing. Set RUNWAYML_API_SECRET or RUNWAY_API_KEY.")
    client = RunwayML(api_key=RUNWAY_API_KEY)


# ---------------- IMAGE OPTIMIZATION ----------------
def optimize_image_for_runway(image_path: str, max_width: int = 1024, max_height: int = 1024) -> str:
    """
    Resize and convert image to JPEG format optimized for RunwayML API.
    Returns path to the optimized image file.
    """
    try:
        with Image.open(image_path) as img:
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")

            width, height = img.size
            if width > max_width or height > max_height:
                ratio = min(max_width / width, max_height / height)
                new_width, new_height = int(width * ratio), int(height * ratio)
                img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)

            temp_path = tempfile.mktemp(suffix=".jpg")
            img.save(temp_path, "JPEG", quality=85, optimize=True)
            return temp_path
    except Exception as e:
        print(f"⚠️ Image optimization failed: {e}")
        return image_path
@router.get("/runway/status")
def runway_status():
    """
    Simple integration status for observability (no external calls).
    """
    return {
        "mock": USE_MOCK_RUNWAY,
        "api_key_present": bool(RUNWAY_API_KEY),
        "model": RUNWAY_MODEL,
        "will_charge": (not USE_MOCK_RUNWAY),
        "videos_dir": os.path.abspath(VIDEOS_DIR),
        "images_dir": os.path.abspath(IMAGES_DIR),
    }


# ----------------------- UPLOAD (MULTI) -----------------------

@router.post("/upload")
async def upload_photos(
    package: str = Form(...),                    # required
    add_ons: Optional[str] = Form(None),         # optional (JSON string from frontend)
    files: List[UploadFile] = File(...)          # required
):
    # Debug print incoming request
    print(f"[DEBUG] Package received: {package}")
    print(f"[DEBUG] Raw add_ons received: {add_ons}")
    print(f"[DEBUG] Number of files received: {len(files)}")
    print(f"[DEBUG] File names: {[file.filename for file in files]}")

    # Validate package
    if package not in PACKAGE_LIMITS:
        print(f"[ERROR] Invalid package: {package}")
        raise HTTPException(status_code=400, detail="Invalid package selected")

    # Parse add_ons if provided
    add_ons_list = []
    if add_ons:
        import json
        try:
            add_ons_list = json.loads(add_ons)
            print(f"[DEBUG] Parsed add_ons: {add_ons_list}")
        except Exception as e:
            print(f"[ERROR] Failed to parse add_ons: {e}")
            raise HTTPException(status_code=400, detail="Invalid add_ons format")

    # Validate file count
    min_files, max_files = PACKAGE_LIMITS[package]
    if not (min_files <= len(files) <= max_files):
        raise HTTPException(
            status_code=400,
            detail=f"{package} allows {min_files}-{max_files} photos"
        )

    print("[DEBUG] Upload validation successful")

    db = SessionLocal()
    results = []

    try:
        # 1) Create Order (NEW)
        order = Order(
            package=package,
            add_ons=",".join(add_ons_list) if add_ons_list else None,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        print(f"[DEBUG] Created Order ID: {order.id}")

        # 2) Process each file
        for file in files:
            src_path = os.path.join(IMAGES_DIR, file.filename)

            print(f"📂 Processing: {file.filename}")
            try:
                print(f"[STEP] Saving file → {src_path}")
                with open(src_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)
                print(f"[OK] Saved file {file.filename}")

                print("[STEP] Opening image with PIL")
                with Image.open(src_path) as im:
                    w, h = im.size
                print(f"[OK] Image size: {w}x{h}")
            except Exception as e:
                print(f"[ERROR] While processing {file.filename}: {e}")
                raise

            # 3) Create UploadedImage row (linked to Order)
            img_row = UploadedImage(
                order_id=order.id,                 # NEW: link to order
                filename=file.filename,
                upload_time=datetime.utcnow(),
            )
            db.add(img_row)
            db.commit()
            db.refresh(img_row)

            # 4) Generate prompt
            prompt_text = generate_cinematic_prompt_from_image(src_path)
            img_row.prompt = prompt_text
            db.commit()

            # 5) Decide aspect ratio
            with Image.open(src_path) as im:
                w, h = im.size
            ratio = "1280:720" if w >= h else "720:1280"

            # 6) Optimize → base64 data URL
            opt_path = optimize_image_for_runway(src_path)
            with open(opt_path, "rb") as rf:
                image_b64 = base64.b64encode(rf.read()).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{image_b64}"

            # 7) Get video (SDK or mock)
            if USE_MOCK_RUNWAY:
                video_filename = f"mock_{img_row.id}.mp4"
                video_path = os.path.join(VIDEOS_DIR, video_filename)
                with open(video_path, "wb") as vf:
                    vf.write(b"")
                video_url = None
                task_id = f"mock-job-{int(datetime.utcnow().timestamp())}"
                status = "succeeded"
            else:
                task = client.image_to_video.create(
                    model=RUNWAY_MODEL,
                    prompt_image=data_url,
                    prompt_text=prompt_text,
                    ratio=ratio,
                ).wait_for_task_output()

                if not task.output:
                    raise HTTPException(status_code=500, detail="RunwayML did not return a video")

                video_url = task.output[0]
                task_id = task.id
                status = "succeeded"

                video_filename = f"video_{img_row.id}.mp4"
                video_path = os.path.join(VIDEOS_DIR, video_filename)
                resp = requests.get(video_url, timeout=300)
                resp.raise_for_status()
                with open(video_path, "wb") as vf:
                    vf.write(resp.content)

            # 8) Save Video row
            video_row = Video(
                image_id=img_row.id,
                prompt=prompt_text,
                runway_job_id=task_id,
                status=status,
                video_url=video_url,
                video_path=video_path,
                iteration=1,
            )
            db.add(video_row)
            db.commit()
            db.refresh(video_row)

            # 9) Update UploadedImage with video info
            img_row.video_path = video_path
            img_row.video_url = video_url
            img_row.video_generated_at = datetime.utcnow()
            db.commit()

            # 10) Response item
            results.append({
                "image_id": img_row.id,
                "filename": file.filename,
                "prompt": prompt_text,
                "video_id": video_row.id,
                "status": status,
                "video_url": video_url,
                "local_path": video_path,
            })

            if opt_path != src_path:
                try:
                    os.remove(opt_path)
                except Exception:
                    pass

        return {
            "status": "success",
            "order_id": order.id,    # NEW
            "package": order.package,
            "add_ons": order.add_ons,
            "results": results
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error processing files: {str(e)}")

    finally:
        db.close()


# ----------------------- FEEDBACK -----------------------
@router.post("/feedback")
def submit_feedback(payload: dict):
    """
    Saves feedback and creates a 'child' Video row with an improved prompt.
    NOTE: Does not auto-regenerate a new video (left as TODO).
    """
    video_id = int(payload.get("video_id", 0))
    feedback_text = (payload.get("feedback_text") or "").strip()

    if not video_id or not feedback_text:
        raise HTTPException(status_code=400, detail="video_id and feedback_text are required")

    db = SessionLocal()
    try:
        parent_video = db.query(Video).filter(Video.id == video_id).first()
        if not parent_video:
            raise HTTPException(status_code=404, detail="Video not found")

        image = db.query(UploadedImage).filter(UploadedImage.id == parent_video.image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Source image not found")

        new_prompt = improve_prompt_with_feedback(parent_video.prompt, feedback_text)

        fb = Feedback(video_id=video_id, feedback_text=feedback_text, new_prompt=new_prompt)
        db.add(fb)
        db.commit()
        db.refresh(fb)

        child = Video(
            image_id=parent_video.image_id,
            prompt=new_prompt,
            parent_video_id=parent_video.id,
            iteration=(parent_video.iteration or 1) + 1,
            status="queued",  # TODO: implement regeneration flow if desired
        )
        db.add(child)
        db.commit()
        db.refresh(child)

        return {"new_video_id": child.id, "status": "queued", "new_prompt": new_prompt}

    finally:
        db.close()


# ----------------------- VIDEO STATUS -----------------------

# @router.get("/video/status/{video_id}")
# def get_video_status(video_id: int):
#     """
#     Returns the stored status for a video job.
#     Since /upload waits for completion (SDK .wait_for_task_output), most videos
#     will already be 'succeeded'. This endpoint simply reflects DB state.
#     """
#     db = SessionLocal()
#     try:
#         video = db.query(Video).filter(Video.id == video_id).first()
#         if not video:
#             raise HTTPException(status_code=404, detail="Video not found")

#         image = db.query(UploadedImage).filter(UploadedImage.id == video.image_id).first()

#         data = {
#             "video_id": video.id,
#             "status": video.status,
#             "prompt": video.prompt,
#             "runway_task_id": video.runway_job_id,
#             "video_url": video.video_url,
#             "video_path": video.video_path,
#             "iteration": video.iteration,
#             "created_at": video.created_at,
#             "image_filename": image.filename if image else None,
#         }

#         if video.video_path and os.path.exists(video.video_path):
#             data["local_url"] = f"/videos/{os.path.basename(video.video_path)}"

#         return data

#     finally:
#         db.close()


# ----------------------- RUNWAY STATUS -----------------------
