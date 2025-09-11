from fastapi import APIRouter, HTTPException, UploadFile, File
from datetime import datetime
import os
import shutil
import time
from app.models.database import SessionLocal, Order, UploadedImage, Video, User
from sqlalchemy.sql import func

router = APIRouter()

# ---------------- ADMIN: ORDER MANAGEMENT ----------------
@router.get("/Admin/order_management", tags=["Admin Portal"])
def get_order_status():
    db = SessionLocal()
    try:
        orders = db.query(Order).order_by(Order.created_at.desc()).all()
        response = []

        for order in orders:
            # Get images for this order
            images = db.query(UploadedImage).filter(UploadedImage.order_id == order.id).all()

            # Get latest video per image (completed output)
            latest_video_subq = (
                db.query(
                    Video.image_id.label("image_id"),
                    func.max(Video.iteration).label("max_iter")
                )
                .group_by(Video.image_id)
                .subquery()
            )
            latest_videos = (
                db.query(Video)
                .join(
                    latest_video_subq,
                    (Video.image_id == latest_video_subq.c.image_id) &
                    (Video.iteration == latest_video_subq.c.max_iter),
                )
                .filter(Video.order_id == order.id)
                .all()
            )

            # Map videos by image
            image_id_to_video = {v.image_id: v for v in latest_videos}

            # Count photos
            photo_count = len(images)

            # Decide status
            if all(v.status == "completed" for v in image_id_to_video.values()):
                status = "completed"
            elif any(v.status == "processing" for v in image_id_to_video.values()):
                status = "processing"
            else:
                status = "submitted"

            response.append({
                "order_id": order.id,
                "client": order.user_id,  # or join User table for name/email
                "package": order.package,
                "add_ons": order.add_ons,
                "photos": photo_count,
                "status": status,
                "date": order.created_at,
                "videos": [
                    {
                        "filename": v.video_path.split("/")[-1],
                        "url": v.video_url,
                        "status": v.status
                    }
                    for v in image_id_to_video.values()
                ],
            })

        return {"orders": response, "count": len(response)}

    finally:
        db.close()


# ----------------------- ADMIN: UPDATE STATUS -----------------------

@router.post("/admin/orders/{image_id}/status", tags=["Admin Portal"])
def admin_update_order_status(order_id: int, payload: dict):
    """Admin sets the latest video's status for an order (UploadedImage).
    
    Accepts statuses like pending|processing|completed|failed.
    Maps 'completed' -> 'succeeded' for internal storage.
    """
    new_status = (payload.get("status") or "").strip().lower()
    status_map = {"completed": "succeeded", "pending": "queued"}
    internal_status = status_map.get(new_status, new_status)
    if internal_status not in {"queued", "processing", "succeeded", "failed"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    db = SessionLocal()
    try:
        # 1️⃣ Get the uploaded image
        image = db.query(UploadedImage).filter(UploadedImage.id == order_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Image not found")

        # 2️⃣ Get the latest video for this image
        latest_video = (
            db.query(Video)
            .filter(Video.image_id == image.id)
            .order_by(Video.iteration.desc(), Video.id.desc())
            .first()
        )

        # 3️⃣ If no video exists, create one
        if not latest_video:
            latest_video = Video(
                image_id=image.id,
                prompt=image.prompt or "",
                status=internal_status or "queued",
                iteration=1,
            )
            db.add(latest_video)
            db.commit()
            db.refresh(latest_video)
        else:
            # Update status if internal_status is provided
            if internal_status:
                latest_video.status = internal_status
                db.commit()

        # 4️⃣ Return correct IDs
        return {
            "image_id": image.id,            # UploadedImage.id
            "video_id": latest_video.id,     # Video.id
            "status": latest_video.status,
            "video_path": latest_video.video_path,
            "video_url": latest_video.video_url,
        }
    finally:
        db.close()


# ----------------------- ADMIN: UPLOAD FINAL VIDEO -----------------------
@router.post("/admin/orders/{image_id}/final-video", tags=["Admin Portal"])
async def admin_upload_final_video(order_id: int, file: UploadFile = File(...)):
    """Admin uploads a final rendered video for an order.
    
    Saves to videos folder and marks the latest Video as succeeded.
    """
    db = SessionLocal()
    try:
        image = db.query(UploadedImage).filter(UploadedImage.id == order_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Order not found")
        
        ts = int(time.time())
        filename = f"final_{order_id}_{ts}.mp4"
        video_path = os.path.join("videos", filename)
        with open(video_path, "wb") as out:
            shutil.copyfileobj(file.file, out)
        
        # Latest video row or create if missing
        latest_video = (
            db.query(Video)
            .filter(Video.image_id == order_id)
            .order_by(Video.iteration.desc(), Video.id.desc())
            .first()
        )
        if not latest_video:
            latest_video = Video(
                image_id=order_id,
                prompt=image.prompt or "",
                iteration=1,
            )
            db.add(latest_video)
            db.commit()
            db.refresh(latest_video)
        
        latest_video.status = "succeeded"
        latest_video.video_path = video_path
        latest_video.video_url = None
        db.commit()
        
        # Mirror onto UploadedImage for convenience
        image.video_path = video_path
        image.video_url = None
        image.video_generated_at = datetime.utcnow()
        db.commit()
        
        return {
            "order_id": image.id,
            "video_id": latest_video.id,
            "status": latest_video.status,
            "local_url": f"/videos/{filename}",
            "video_path": video_path,
        }
    finally:
        db.close()
