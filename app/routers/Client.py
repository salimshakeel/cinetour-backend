from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from typing import List, Optional
from datetime import datetime
import shutil, os
from app.routers.auth import get_current_user

from app.models.database import SessionLocal, Order, UploadedImage, Video, Invoice, User , Payment, FinalVideo

router = APIRouter(tags=["Client Portal"])

# ---------------- DB Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------- CLIENT: STATUS ----------------

@router.get("/client/status")
def client_status(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Returns whether the user has any orders and basic info about them,
    including related videos (if available).
    """
    orders = db.query(Order).filter(Order.user_id == current_user.id).all()

    if not orders:
        return {
            "has_orders": False,
            "user_email": current_user.email,
            "user_name": current_user.name
        }

    response_orders = []

    for order in orders:
        # get all uploaded images for this order
        images_data = []
        for img in order.images:
            # collect related video info
            videos = db.query(Video).filter(Video.image_id == img.id).all()
            videos_data = [
                {
                    "id": v.id,
                    "prompt": v.prompt,
                    "status": v.status,
                    "video_url": v.video_url,
                    "created_at": v.created_at
                }
                for v in videos
            ]

            images_data.append({
                "id": img.id,
                "filename": img.filename,
                "video_url": img.video_url,
                "video_generated_at": img.video_generated_at,
                "videos": videos_data
            })

        response_orders.append({
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "created_at": order.created_at,
            "images": images_data
        })

    return {
        "has_orders": True,
        "user_email": current_user.email,
        "user_name": current_user.name,
        "orders": response_orders
    }


# ---------------- 1. DOWNLOAD CENTER ----------------
# @router.get("/download-center")
# def get_download_center(
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db)
# ):
#     """
#     Returns only final (completed/succeeded) videos for the currently logged-in client.
#     """
#     user_id = current_user.id

#     # Get all orders for this user
#     orders = (
#         db.query(Order)
#         .filter(Order.user_id == user_id)
#         .order_by(Order.created_at.desc())
#         .all()
#     )

#     response = []

#     for order in orders:
#         # Get latest completed/succeeded videos for images in this order
#         latest_video_subq = (
#             db.query(
#                 Video.image_id.label("image_id"),
#                 func.max(Video.iteration).label("max_iter")
#             )
#             .group_by(Video.image_id)
#             .subquery()
#         )

#         completed_videos = (
#             db.query(Video)
#             .join(
#                 latest_video_subq,
#                 (Video.image_id == latest_video_subq.c.image_id) &
#                 (Video.iteration == latest_video_subq.c.max_iter)
#             )
#             .join(UploadedImage, UploadedImage.id == Video.image_id)
#             .filter(
#                 UploadedImage.order_id == order.id,
#                 Video.status.in_(["completed", "succeeded"])
#             )
#             .all()
#         )

#         # Build response for each completed video
#         videos_info = []
#         for v in completed_videos:
#             if v.video_url:
#                 # Convert Dropbox URL to direct link (so frontend can preview/download)
#                 direct_url = v.video_url.replace("?dl=0", "?raw=1")
#                 videos_info.append({
#                     "filename": v.video_path.split("/")[-1] if v.video_path else None,
#                     "direct_url": direct_url,
#                     "dropbox_url": v.video_url,
#                     "status": v.status
#                 })

#         if videos_info:
#             response.append({
#                 "order_id": order.id,
#                 "package": order.package,
#                 "add_ons": order.add_ons,
#                 "date": order.created_at.isoformat(),
#                 "videos": videos_info
#             })

#     return {
#         "user_email": current_user.email,
#         "downloads": response,
#         "count": len(response)
#     }

@router.get("/download-center")
def get_download_center(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user_id = current_user.id

    final_videos = (
        db.query(FinalVideo)
        .filter(FinalVideo.user_id == user_id)
        .order_by(FinalVideo.created_at.desc())
        .all()
    )

    downloads = [
        {
            "video_id": v.id,
            "filename": os.path.basename(v.dropbox_path),
            "url": v.video_url,
            "created_at": v.created_at.isoformat() if v.created_at else None
        }
        for v in final_videos
    ]

    return {
        "user_email": current_user.email,
        "downloads": downloads,
        "count": len(downloads)
    }


# ---------------- 2. NEW ORDER ----------------
@router.post("/orders/new")
async def create_new_order(
    user_id: int = Form(...),
    package: str = Form(...),
    add_ons: Optional[str] = Form(None),
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db)
):
    """Create a new order + upload images + generate invoice."""
    order = Order(user_id=user_id, package=package, add_ons=add_ons)
    db.add(order)
    db.commit()
    db.refresh(order)

    # Save uploaded images
    upload_dir = "uploads"
    os.makedirs(upload_dir, exist_ok=True)

    for file in files:
        file_path = os.path.join(upload_dir, file.filename)
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        image = UploadedImage(
            order_id=order.id,
            filename=file.filename,
            upload_time=datetime.utcnow()
        )
        db.add(image)

    db.commit()

    # Create invoice
    invoice = Invoice(order_id=order.id, user_id=user_id, amount=100, is_paid=False)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    return {
        "message": "Order created successfully",
        "order": {
            "id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "date": order.created_at.isoformat(),
        },
        "invoice": {
            "id": invoice.id,
            "amount": invoice.amount,
            "status": "unpaid"
        }
    }
@router.get("/orders/status")
def get_client_orders(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Fetch all orders for the currently authenticated client 
    with their current status (submitted, processing, completed).
    """
    user_id = current_user.id  # Automatically detect user from token/session

    orders = (
        db.query(Order)
        .filter(Order.user_id == user_id)
        .order_by(Order.created_at.desc())
        .all()
    )

    if not orders:
        raise HTTPException(status_code=44, detail="No orders found for this client.")

    # Subquery to get only the latest video per image
    latest_video_subq = (
        db.query(Video.image_id, func.max(Video.iteration).label("max_iter"))
        .group_by(Video.image_id)
        .subquery()
    )

    response = []
    for order in orders:
        images = db.query(UploadedImage).filter(UploadedImage.order_id == order.id).all()

        latest_videos = (
            db.query(Video)
            .join(
                latest_video_subq,
                (Video.image_id == latest_video_subq.c.image_id)
                & (Video.iteration == latest_video_subq.c.max_iter),
            )
            .join(UploadedImage, UploadedImage.id == Video.image_id)
            .filter(UploadedImage.order_id == order.id)
            .all()
        )

        image_id_to_video = {v.image_id: v for v in latest_videos}
        statuses = [v.status for v in image_id_to_video.values()]

        if all(s == "succeeded" for s in statuses):
            status = "completed"
        elif any(s == "processing" for s in statuses):
            status = "processing"
        else:
            status = "submitted"

        response.append({
            "order_id": order.id,
            "package": order.package,
            "add_ons": order.add_ons,
            "status": status,
            "date": order.created_at.isoformat(),
            "videos": [
                {
                    "filename": v.video_path.split("/")[-1] if v.video_path else None,
                    "url": v.video_url or "",
                    "status": v.status,
                }
                for v in image_id_to_video.values()
            ],
        })

    return {"orders": response, "count": len(response)}
# ---------------- 3. REORDER ----------------
@router.post("/orders/{order_id}/reorder")
def reorder(order_id: int, db: Session = Depends(get_db)):
    """Reorder: create a new order linked to a previous one."""
    old_order = db.query(Order).filter(Order.id == order_id).first()
    if not old_order:
        raise HTTPException(status_code=404, detail="Order not found")

    new_order = Order(
        user_id=old_order.user_id,
        package=old_order.package,
        add_ons=old_order.add_ons,
        parent_order_id=old_order.id
    )
    db.add(new_order)
    db.commit()
    db.refresh(new_order)

    invoice = Invoice(order_id=new_order.id, user_id=old_order.user_id, amount=100, is_paid=False)
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    return {
        "message": "Reorder created successfully",
        "order": {
            "id": new_order.id,
            "linked_to": old_order.id,
            "package": new_order.package,
            "add_ons": new_order.add_ons
        },
        "invoice": {
            "id": invoice.id,
            "amount": invoice.amount,
            "status": "unpaid"
        }
    }

@router.get("/invoices")
def get_client_invoices(
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns all invoices for the logged-in user,
    including Stripe payment info and order details.
    """
    invoices = (
        db.query(Invoice)
        .filter(Invoice.user_id == current_user.id)
        .all()
    )

    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices found")

    response = []

    for inv in invoices:
        payment = (
            db.query(Payment)
            .filter(Payment.order_id == inv.order_id)
            .order_by(Payment.created_at.desc())
            .first()
        )

        order = db.query(Order).filter(Order.id == inv.order_id).first()

        response.append({
            "invoice_id": inv.id,
            "order_id": inv.order_id,
            "amount": inv.amount,
            "currency": payment.currency if payment else "usd",
            "status": payment.status if payment else inv.status,
            "is_paid": True if (payment and payment.status == "succeeded") else False,
            "created_at": inv.created_at,
            "due_date": inv.due_date,
            "order_info": {
                "package": order.package if order else None,
                "addons": order.add_ons if order else None,
                "created_at": order.created_at if order else None
            },
            "stripe_metadata": payment.payment_metadata if payment else None,
        })

    return {"user": current_user.email, "invoices": response}