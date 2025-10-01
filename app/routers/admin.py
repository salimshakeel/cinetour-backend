from fastapi import APIRouter, HTTPException, UploadFile, File
from datetime import datetime
import os
import shutil
import time
from app.models.database import SessionLocal, Order, UploadedImage, Video, User ,Notification
from sqlalchemy.sql import func
from app.services.runway_service import generate_video
from datetime import timedelta
router = APIRouter()
# ---------------- ADMIN: VIDEOS LISTING ----------------
@router.get("/admin/videos", tags=["Admin Portal"])
def list_videos():
    """Return latest videos with playable/downloadable URLs and related client/image info."""
    db = SessionLocal()
    try:
        videos = db.query(Video).order_by(Video.created_at.desc()).limit(200).all()

        items = []
        for v in videos:
            filename = os.path.basename(v.video_path) if v.video_path else None
            local_url = f"/videos/{filename}" if filename else None
            # fetch image and order for context
            image = db.query(UploadedImage).filter(UploadedImage.id == v.image_id).first()
            order = db.query(Order).filter(Order.id == image.order_id).first() if image else None
            client_id = order.user_id if order else None
            # build image public url
            image_url = f"/uploaded_images/{image.filename}" if image and image.filename else None
            items.append({
                "video_id": v.id,
                "image_id": v.image_id,
                "client_id": client_id,
                "status": v.status,
                "prompt": v.prompt,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "iteration": v.iteration,
                "runway_job_id": v.runway_job_id,
                "remote_url": v.video_url,
                "local_url": local_url,
                "filename": filename,
                "download_url": local_url,  # same as local_url; frontend can use download attribute
                "image_filename": image.filename if image else None,
                "image_url": image_url,
            })

        return {"videos": items, "count": len(items)}
    finally:
        db.close()


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
            # latest video per image subquery
            latest_video_subq = (
                db.query(
                    Video.image_id,
                    func.max(Video.iteration).label("max_iter")
                )
                .group_by(Video.image_id)
                .subquery()
            )

            # get latest videos for this order
            latest_videos = (
                db.query(Video)
                .join(
                    latest_video_subq,
                    (Video.image_id == latest_video_subq.c.image_id) &
                    (Video.iteration == latest_video_subq.c.max_iter),
                )
                .join(UploadedImage, UploadedImage.id == Video.image_id)  # ✅ join UploadedImage
                .filter(UploadedImage.order_id == order.id)                # ✅ filter by UploadedImage.order_id
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


# ----------------------- ADMIN: CUSTOMIZE PROMPT & REGENERATE -----------------------
@router.post("/admin/orders/{image_id}/regenerate", tags=["Admin Portal"])
def admin_regenerate_video(image_id: int, payload: dict):
    """Regenerate a video's latest iteration from a custom prompt using RunwayML."""
    new_prompt = (payload.get("prompt") or "").strip()
    if not new_prompt:
        raise HTTPException(status_code=422, detail="Prompt is required")

    db = SessionLocal()
    try:
        image = db.query(UploadedImage).filter(UploadedImage.id == image_id).first()
        if not image:
            raise HTTPException(status_code=404, detail="Image not found")

        # Determine input and output paths
        image_path = os.path.join("uploads", image.filename)
        if not os.path.exists(image_path):
            raise HTTPException(status_code=404, detail="Source image file not found on server")

        ts = int(time.time())
        out_filename = f"regen_{image_id}_{ts}.mp4"
        out_path = os.path.join("videos", out_filename)

        # Create Video record first (with queued status)
        video = Video(
            image_id=image.id,
            prompt=new_prompt,
            iteration=next_iteration,
            status="queued",
            runway_job_id=None
        )
        db.add(video)
        db.commit()
        db.refresh(video)

        # Call RunwayML with video_id for real-time tracking
        try:
            gen_result = generate_video(
                prompt=new_prompt, 
                image_path=image_path, 
                output_path=out_path,
                video_id=video.id
            )
        except Exception as e:
            
        # Update video status to failed
            video.status = "failed"
            video.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(status_code=500, detail=str(e))

        # Mirror basic info onto UploadedImage for convenience
        image.video_path = out_path
        image.video_url = gen_result.get("video_url")
        image.video_generated_at = datetime.utcnow()
        db.commit()

        return {
            "image_id": image.id,
            "video_id": video.id,
            "iteration": video.iteration,
            "status": video.status,
            "video_path": video.video_path,
            "video_url": video.video_url,
        }
    finally:
        db.close()


# ----------------------- ADMIN: LOGS & STATUS -----------------------
@router.get("/admin/logs-status", tags=["Admin Portal"])
def admin_logs_status():
    """Return real-time video processing status and detailed logs."""
    db = SessionLocal()
    try:
        # Summary by status
        status_counts = {
            "queued": db.query(Video).filter(Video.status == "queued").count(),
            "processing": db.query(Video).filter(Video.status == "processing").count(),
            "succeeded": db.query(Video).filter(Video.status == "succeeded").count(),
            "failed": db.query(Video).filter(Video.status == "failed").count(),
        }

        # Get all videos with detailed processing info
        videos = (
            db.query(Video)
            .join(UploadedImage, Video.image_id == UploadedImage.id)
            .join(Order, UploadedImage.order_id == Order.id)
            .order_by(Video.created_at.desc())
            .limit(50)
            .all()
        )

        # Build detailed logs with processing timeline
        logs = []
        for v in videos:
            # Get the uploaded image and order info
            image = db.query(UploadedImage).filter(UploadedImage.id == v.image_id).first()
            order = db.query(Order).filter(Order.id == image.order_id).first() if image else None
            
            # Calculate processing time
            processing_time = None
            if v.status == "succeeded" and v.created_at:
                processing_time = (datetime.utcnow() - v.created_at).total_seconds()
            
            # Determine current stage
            stage = "unknown"
            if v.status == "queued":
                stage = "Waiting in queue"
            elif v.status == "processing":
                stage = "Generating video with AI"
            elif v.status == "succeeded":
                stage = "Video completed"
            elif v.status == "failed":
                stage = "Generation failed"
            
            logs.append({
                "video_id": v.id,
                "image_id": v.image_id,
                "order_id": order.id if order else None,
                "iteration": v.iteration,
                "status": v.status,
                "stage": stage,
                "prompt": v.prompt[:100] + "..." if v.prompt and len(v.prompt) > 100 else v.prompt,
                "video_path": v.video_path,
                "video_url": v.video_url,
                "runway_job_id": v.runway_job_id,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "updated_at": v.updated_at.isoformat() if v.updated_at else None,
                "processing_time_seconds": processing_time,
                "client_email": order.user.email if order and order.user else "Guest",
                "package": order.package if order else "Unknown"
            })

        # Get currently processing videos (for real-time updates)
        processing_videos = [
            {
                "video_id": v.id,
                "image_id": v.image_id,
                "prompt": v.prompt[:50] + "..." if v.prompt and len(v.prompt) > 50 else v.prompt,
                "started_at": v.created_at.isoformat() if v.created_at else None,
                "elapsed_seconds": (datetime.utcnow() - v.created_at).total_seconds() if v.created_at else 0,
                "runway_job_id": v.runway_job_id
            }
            for v in db.query(Video).filter(Video.status == "processing").all()
        ]

        return {
            "status": status_counts,
            "logs": logs,
            "processing_now": processing_videos,
            "last_updated": datetime.utcnow().isoformat()
        }
    finally:
        db.close()


# ----------------------- ADMIN: NOTIFICATIONS -----------------------
@router.get("/admin/notifications", tags=["Admin Portal"])
def admin_notifications():
    """Unified admin notifications for UI with user emails via relationship."""
    db = SessionLocal()
    try:
        notifications = []

        # 1️⃣ Video processing notifications
        failed_count = db.query(Video).filter(Video.status == "failed").count()
        processing_count = db.query(Video).filter(Video.status == "processing").count()
        succeeded_videos = (
            db.query(Video)
            .filter(Video.status == "succeeded")
            .order_by(Video.updated_at.desc())
            .limit(5)
            .all()
        )

        if failed_count:
            notifications.append({
                "type": "video_failed",
                "message": f"{failed_count} video jobs failed. Review and retry.",
                "category": "video_processing"
            })

        if processing_count:
            notifications.append({
                "type": "video_processing",
                "message": f"{processing_count} video jobs currently processing.",
                "category": "video_processing"
            })

        for v in succeeded_videos:
            user_email = "Guest"
            user_id = None

            if v.image and v.image.order and v.image.order.user:
                user_email = v.image.order.user.email or "Guest"
                user_id = v.image.order.user.id

            notifications.append({
                "type": "video_completed",
                "message": f"Video #{v.id} for order #{v.image.order_id} completed.",
                "video_id": v.id,
                "video_path": v.video_path,
                "image_id": v.image_id,
                "order_id": v.image.order_id,
                "user_id": v.image.order.user_id,
                "user_email": user_email,
                "category": "video_processing"
            })

        # 2️⃣ Notifications from the Notification table (new users, etc.)
        recent_notifications = db.query(Notification).order_by(Notification.created_at.desc()).limit(20).all()
        for notif in recent_notifications:
            notifications.append({
                "type": notif.type,
                "message": notif.message,
                "user_id": notif.user_id,
                "user_email": notif.user.email if notif.user else "Guest",
                "is_read": notif.is_read,
                "created_at": notif.created_at.isoformat(),
                "category": "system_notifications"
            })

        # 3️⃣ System stats
        total_users = db.query(User).count()
        total_orders = db.query(Order).count()
        total_videos = db.query(Video).count()

        notifications.append({
            "type": "system_stats",
            "message": f"System stats: {total_users} users, {total_orders} orders, {total_videos} videos",
            "stats": {
                "users": total_users,
                "orders": total_orders,
                "videos": total_videos
            },
            "category": "system_stats"
        })

        return {"notifications": notifications}
    finally:
        db.close()