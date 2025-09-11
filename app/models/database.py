from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, ForeignKey , Boolean
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

DATABASE_URL = "sqlite:///./uploaded_images.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
Base = declarative_base()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)   # if guest, keep null
    package = Column(String, nullable=False)
    add_ons = Column(String, nullable=True)    # JSON string or comma-separated
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    parent_order_id = Column(Integer, ForeignKey("orders.id"), nullable=True)  
    # 👆 if this is a reorder, link back to original order

    # Relationships
    images = relationship("UploadedImage", back_populates="order")
    invoice = relationship("Invoice", back_populates="order", uselist=False)
    user = relationship("User", back_populates="orders")
    parent_order = relationship("Order", remote_side=[id])

class UploadedImage(Base):
    __tablename__ = "uploaded_images"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)

    filename = Column(String, nullable=False)
    prompt = Column(String, nullable=True)
    video_path = Column(String, nullable=True)
    video_url = Column(String, nullable=True)
    upload_time = Column(DateTime(timezone=True), server_default=func.now())
    video_generated_at = Column(DateTime(timezone=True), nullable=True)

    # link back to Order
    order = relationship("Order", back_populates="images")

class Video(Base):
    __tablename__ = "videos"

    id = Column(Integer, primary_key=True, index=True)
    image_id = Column(Integer, ForeignKey("uploaded_images.id"), nullable=False)
    prompt = Column(Text, nullable=False)
    runway_job_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="queued")  # queued|processing|succeeded|failed
    video_url = Column(String, nullable=True)
    video_path = Column(String, nullable=True)
    parent_video_id = Column(Integer, ForeignKey("videos.id"), nullable=True)
    iteration = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow)

class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True)
    video_id = Column(Integer, ForeignKey("videos.id"), nullable=False)
    feedback_text = Column(Text, nullable=False)
    new_prompt = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Extend User model
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=True)   # nullable if guest
    password_hash = Column(String, nullable=True)        # null for guest
    is_guest = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    orders = relationship("Order", back_populates="user")
    invoices = relationship("Invoice", back_populates="user")

class Invoice(Base):
    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    amount = Column(Integer, nullable=False)
    status = Column(String, default="unpaid")  # unpaid | paid | refunded
    due_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    order = relationship("Order", back_populates="invoice")
    user = relationship("User", back_populates="invoices")
