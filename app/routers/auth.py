from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr
from app.models.database import SessionLocal, User
from datetime import datetime, timedelta
import hashlib
import os
from app.services.security import create_access_token
from app.config import ACCESS_TOKEN_EXPIRE_MINUTES

router = APIRouter()


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class SignupPayload(BaseModel):
    email: EmailStr
    password: str


class SigninPayload(BaseModel):
    email: EmailStr
    password: str


def serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "is_guest": bool(u.is_guest),
        "created_at": u.created_at,
    }


# ------------------- SIGNUP -------------------
@router.post("/signup")
def signup(payload: SignupPayload):
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == payload.email).first()
        if existing and not existing.is_guest:
            raise HTTPException(status_code=400, detail="Email already registered")

        salt = os.getenv("AUTH_SALT", "static_salt")
        pw_hash = hash_password(payload.password, salt)

        if existing and existing.is_guest:
            # upgrade guest -> full user
            existing.email = payload.email
            existing.password_hash = pw_hash
            existing.is_guest = False
            db.commit()
            db.refresh(existing)

            access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
            token = create_access_token(
                data={"user_id": existing.id, "email": existing.email},
                expires_delta=access_token_expires,
            )
            return {"user": serialize_user(existing), "access_token": token, "token_type": "bearer"}

        # new user
        user = User(
            email=payload.email,
            password_hash=pw_hash,
            is_guest=False,
            created_at=datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token = create_access_token(
            data={"user_id": user.id, "email": user.email},
            expires_delta=access_token_expires,
        )
        return {"user": serialize_user(user), "access_token": token, "token_type": "bearer"}
    finally:
        db.close()


# ------------------- SIGNIN -------------------
@router.post("/signin")
def signin(payload: SigninPayload):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == payload.email).first()
        if not user or not user.password_hash:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        salt = os.getenv("AUTH_SALT", "static_salt")
        if user.password_hash != hash_password(payload.password, salt):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token = create_access_token(
            data={"user_id": user.id, "email": user.email},
            expires_delta=access_token_expires,
        )

        return {"user": serialize_user(user), "access_token": token, "token_type": "bearer"}
    finally:
        db.close()


# ------------------- GUEST -------------------
@router.post("/guest")
def create_guest():
    db = SessionLocal()
    try:
        user = User(email=None, password_hash=None, is_guest=True, created_at=datetime.utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)

        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        token = create_access_token(
            data={"user_id": user.id, "is_guest": True},
            expires_delta=access_token_expires,
        )

        return {"user": serialize_user(user), "access_token": token, "token_type": "bearer"}
    finally:
        db.close()
