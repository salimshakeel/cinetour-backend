import os
import base64
import shutil
import tempfile
from datetime import datetime
import requests
import dropbox
from fastapi import APIRouter, UploadFile, File, HTTPException, Form
from sqlalchemy.orm import Session
from PIL import Image
from dotenv import load_dotenv
from typing import List, Optional
from pydantic import BaseModel
from runwayml import RunwayML
from dropbox.exceptions import ApiError
from fastapi import BackgroundTasks
from app.models.database import SessionLocal, UploadedImage, Video, Feedback, Order, Notification
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
USE_MOCK_RUNWAY = str(os.getenv("RUNWAY_MOCK", "False")).lower() in {"1", "true", "yes"}

# API key for SDK (supports either env var name)
RUNWAY_API_KEY = os.getenv("RUNWAYML_API_SECRET") or os.getenv("RUNWAY_API_KEY")
RUNWAY_MODEL = os.getenv("RUNWAY_MODEL", "gen4_turbo")

# In your routers file
# Project root
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # points to 'routers' folder
BASE_DIR = os.path.dirname(BASE_DIR)                   # go up to project root

# Correct paths
IMAGES_DIR = os.path.join(BASE_DIR, "uploaded_images")  # where files really exist
VIDEOS_DIR = os.path.join(BASE_DIR, "videos")

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(VIDEOS_DIR, exist_ok=True)




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
    
# ----------------------Notification--------------------

def create_notification(db: Session, user_id: int, type_: str, message: str):
    notif = Notification(
        user_id=user_id,
        type=type_,
        message=message
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif

def upload_video_to_dropbox(video_url: str, dropbox_path: str) -> bool:
    """
    Uploads a video directly from a URL to Dropbox without saving locally.
    Uses refresh-token authentication for permanent access.
    """
    try:
        print(f"[DEBUG] Initializing Dropbox client with refresh token")

        DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY")
        DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET")
        DROPBOX_REFRESH_TOKEN = os.getenv("DROPBOX_REFRESH_TOKEN")

        if not all([DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN]):
            raise Exception("Missing Dropbox credentials in environment variables")

        dbx = dropbox.Dropbox(
            app_key=DROPBOX_APP_KEY,
            app_secret=DROPBOX_APP_SECRET,
            oauth2_refresh_token=DROPBOX_REFRESH_TOKEN
        )

        print(f"[DEBUG] Downloading video from URL → {video_url}")
        resp = requests.get(video_url, timeout=300)
        resp.raise_for_status()
        video_bytes = resp.content

        print(f"[DEBUG] Uploading video to Dropbox → {dropbox_path}")
        dbx.files_upload(video_bytes, dropbox_path, mode=dropbox.files.WriteMode.overwrite)

        print(f"[OK] Uploaded successfully to Dropbox → {dropbox_path}")
        return True

    except ApiError as api_err:
        print(f"[ERROR] Dropbox API error: {api_err}")
        return False
    except Exception as e:
        print(f"[ERROR] Dropbox upload failed: {e}")
        return False
# ----------------------- UPLOAD (MULTI) -----------------------
def process_videos_for_order(order_id: int, file_paths: list):
    print(f"[BG] Start processing order {order_id} with {len(file_paths)} files")
    db: Session = SessionLocal()
    try:
        order = db.query(Order).filter(Order.id == order_id).first()
        if not order:
            print(f"[ERROR] Order {order_id} not found")
            return

        for src_path in file_paths:
            filename = os.path.basename(src_path)
            try:
                print(f"[STEP] Opening image → {src_path}")
                with Image.open(src_path) as im:
                    w, h = im.size
                print(f"[OK] Image size: {w}x{h}")
            except Exception as e:
                print(f"[ERROR] While processing {filename}: {e}")
                continue

            with open(src_path, "rb") as f:
                file_content = f.read()

            # Create UploadedImage row
            img_row = UploadedImage(
                order_id=order.id,
                filename=filename,
                content=file_content,
                upload_time=datetime.utcnow(),
            )
            db.add(img_row)
            db.commit()
            db.refresh(img_row)

            print(f"[STEP] Generating cinematic prompt for {filename}")
            prompt_text = generate_cinematic_prompt_from_image(src_path)
            img_row.prompt = prompt_text
            db.commit()

            ratio = "1280:720" if w >= h else "720:1280"
            opt_path = optimize_image_for_runway(src_path)
            with open(opt_path, "rb") as rf:
                image_b64 = base64.b64encode(rf.read()).decode("utf-8")
            data_url = f"data:image/jpeg;base64,{image_b64}"

            # RunwayML video generation
            if USE_MOCK_RUNWAY:
                video_filename = f"mock_{img_row.id}.mp4"
                video_url, task_id, status = None, f"mock-job-{int(datetime.utcnow().timestamp())}", "succeeded"
            else:
                try:
                    print(f"[STEP] Sending request to RunwayML for {filename}")
                    task = client.image_to_video.create(
                        model=RUNWAY_MODEL,
                        prompt_image=data_url,
                        prompt_text=prompt_text,
                        duration=5,
                        ratio=ratio,
                    ).wait_for_task_output()

                    if not task.output:
                        raise Exception("RunwayML did not return a video")

                    video_url = task.output[0]
                    task_id = task.id
                    status = "succeeded"
                    dropbox_path = f"/videos/video_{img_row.id}.mp4"
                    upload_success = upload_video_to_dropbox(video_url, dropbox_path)

                    if upload_success:
                        video_url = f"dropbox://{dropbox_path}"
                    else:
                        video_url = None
                        status = "failed"

                except Exception as e:
                    print(f"[ERROR] RunwayML generation failed: {e}")
                    status, video_url, task_id = "failed", None, None

            # Save Video row
            video_row = Video(
                user_id=order.user_id,          # 👈 FIXED: link video to the same user as order
                image_id=img_row.id,
                prompt=prompt_text,
                runway_job_id=task_id,
                status=status,
                video_url=video_url,
                iteration=1,
            )
            db.add(video_row)
            db.commit()
            db.refresh(video_row)

            # Update UploadedImage with video info
            img_row.video_url = video_url
            img_row.video_generated_at = datetime.utcnow()
            db.commit()

            if video_row.status == "succeeded":
                create_notification(
                    db=db,
                    user_id=order.user_id,
                    type_="video_created",
                    message=f"Video #{video_row.id} created for Order #{order.id} ({filename})"
                )

            if opt_path != src_path:
                try:
                    os.remove(opt_path)
                except Exception:
                    pass

        print(f"[OK] Finished background processing for order {order_id}")
    finally:
        db.close()
        

@router.post("/upload")
async def upload_photos(
    background_tasks: BackgroundTasks,
    package: str = Form(...),
    add_ons: Optional[str] = Form(None),
    files: List[UploadFile] = File(...)
):
    # if package not in PACKAGE_LIMITS:
    #     raise HTTPException(status_code=400, detail="Invalid package selected")

    # # ✅ Validate number of files
    # min_files, max_files = PACKAGE_LIMITS[package]
    # if not (min_files <= len(files) <= max_files):
    #     raise HTTPException(
    #         status_code=400,
    #         detail=f"{package} allows {min_files}-{max_files} photos"
    #     )
    db = SessionLocal()
    try:
        # create order
        order = Order(
            package=package,
            add_ons=add_ons
        )
        db.add(order)
        db.commit()
        db.refresh(order)

        saved_files = []
        for file in files:
            dst_path = os.path.join(IMAGES_DIR, file.filename)
            print(f"[STEP] Saving file → {dst_path}")
            try:
                with open(dst_path, "wb") as f:
                    shutil.copyfileobj(file.file, f)   # save immediately
                print(f"[OK] Saved file {file.filename}")
                saved_files.append(dst_path)
            except Exception as e:
                print(f"[ERROR] Could not save {file.filename}: {e}")
                raise HTTPException(status_code=500, detail=f"Failed to save {file.filename}")

        # ✅ Pass order.id + saved file paths (not UploadFile objects!)
        background_tasks.add_task(process_videos_for_order, order.id, saved_files)

        return {
            "status": "success",
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "image_id": Video.image_id
        }

    finally:
        db.close()


# ----------------------- FEEDBACK -----------------------
class FeedbackPayload(BaseModel):
    video_id: int
    feedback_text: str

@router.post("/feedback")
def submit_feedback(payload: FeedbackPayload):
    db = SessionLocal()
    try:
        # 1) Get parent video
        parent_video = db.query(Video).filter(Video.id == payload.video_id).first()
        if not parent_video:
            raise HTTPException(status_code=404, detail="Video not found")

        # 2) Get source image
        image = db.query(UploadedImage).filter(UploadedImage.id == parent_video.image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Source image not found")

        # 3) Generate improved prompt
        new_prompt = improve_prompt_with_feedback(parent_video.prompt, payload.feedback_text)

        # 4) Save feedback
        fb = Feedback(video_id=parent_video.id, feedback_text=payload.feedback_text, new_prompt=new_prompt)
        db.add(fb)
        db.commit()
        db.refresh(fb)

        # 5) Prepare image for Runway
        image_b64 = base64.b64encode(image.content).decode("utf-8")
        opt_path = f"data:image/jpeg;base64,{image_b64}"
        with open(opt_path, "rb") as rf:
            image_b64 = base64.b64encode(rf.read()).decode("utf-8")
        data_url = f"data:image/jpeg;base64,{image_b64}"

        # 6) Decide aspect ratio
        with Image.open(opt_path) as im:
            w, h = im.size
        ratio = "1280:720" if w >= h else "720:1280"

        # 7) Generate new video via Runway
        if USE_MOCK_RUNWAY:
            video_filename = f"mock_{image.id}_{int(datetime.utcnow().timestamp())}.mp4"
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
                prompt_text=new_prompt,
                duration=5,
                ratio=ratio,
            ).wait_for_task_output()

            if not task.output:
                raise HTTPException(status_code=500, detail="RunwayML did not return a video")

            video_url = task.output[0]
            task_id = task.id
            status = "succeeded"

            video_filename = f"video_{image.id}_{int(datetime.utcnow().timestamp())}.mp4"
            video_path = os.path.join(VIDEOS_DIR, video_filename)
            resp = requests.get(video_url, timeout=300)
            resp.raise_for_status()
            with open(video_path, "wb") as vf:
                vf.write(resp.content)

        # 8) Create child Video row
        child = Video(
            image_id=image.id,
            prompt=new_prompt,
            parent_video_id=parent_video.id,
            iteration=(parent_video.iteration or 1) + 1,
            runway_job_id=task_id,
            status=status,
            video_url=video_url,
            video_path=video_path,
        )
        db.add(child)
        db.commit()
        db.refresh(child)

        return {
            "new_video_id": child.id,
            "status": status,
            "video_url": video_url,
            "local_path": video_path,
            "new_prompt": new_prompt
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error generating video: {str(e)}")
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
