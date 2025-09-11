from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.sql import func
from typing import List, Optional
from datetime import datetime
import shutil, os

from app.models.database import SessionLocal, Order, UploadedImage, Video, Invoice, User

router = APIRouter(tags=["Client Portal"])

# ---------------- DB Dependency ----------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---------------- 1. DOWNLOAD CENTER ----------------
@router.get("/download-center")
def get_download_center(user_id: int, db: Session = Depends(get_db)):
    """
    Returns only completed videos for a specific client (user).
    """
    orders = db.query(Order).filter(Order.user_id == user_id).order_by(Order.created_at.desc()).all()
    response = []

    for order in orders:
        # Get only latest completed videos per image
        latest_video_subq = (
            db.query(
                Video.image_id.label("image_id"),
                func.max(Video.iteration).label("max_iter")
            )
            .group_by(Video.image_id)
            .subquery()
        )

        completed_videos = (
            db.query(Video)
            .join(
                latest_video_subq,
                (Video.image_id == latest_video_subq.c.image_id) &
                (Video.iteration == latest_video_subq.c.max_iter),
            )
            .filter(Video.order_id == order.id, Video.status == "completed")
            .all()
        )

        if completed_videos:
            response.append({
                "order_id": order.id,
                "package": order.package,
                "add_ons": order.add_ons,
                "date": order.created_at.isoformat(),
                "videos": [
                    {
                        "filename": v.video_path.split("/")[-1] if v.video_path else None,
                        "url": v.video_url
                    }
                    for v in completed_videos
                ],
            })

    return {"downloads": response, "count": len(response)}

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

# ---------------- 4. INVOICES ----------------
@router.get("/{user_id}/invoices")
def get_invoices(user_id: int, db: Session = Depends(get_db)):
    """Get all invoices for a user."""
    invoices = db.query(Invoice).filter(Invoice.user_id == user_id).all()
    return {
        "invoices": [
            {
                "id": inv.id,
                "order_id": inv.order_id,
                "amount": inv.amount,
                "status": "paid" if inv.is_paid else "unpaid",
                "date": inv.created_at.isoformat()
            }
            for inv in invoices
        ]
    }

@router.get("/invoice/{invoice_id}")
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    """Get details of a single invoice."""
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    return {
        "id": invoice.id,
        "order_id": invoice.order_id,
        "amount": invoice.amount,
        "status": "paid" if invoice.is_paid else "unpaid",
        "date": invoice.created_at.isoformat(),
        "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None
    }

@router.post("/invoice/{order_id}/pay")
def pay_invoice(order_id: int, db: Session = Depends(get_db)):
    """Mark invoice as paid (later integrate Stripe/PayPal here)."""
    invoice = db.query(Invoice).filter(Invoice.order_id == order_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

    if invoice.is_paid:
        return {"message": "Invoice already paid", "invoice_id": invoice.id}

    invoice.is_paid = True
    invoice.paid_at = datetime.utcnow()
    db.commit()
    return {
        "message": "Invoice paid successfully",
        "invoice": {
            "id": invoice.id,
            "status": "paid",
            "paid_at": invoice.paid_at.isoformat()
        }
    }
