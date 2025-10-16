from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from google.oauth2 import id_token
from google.auth.transport import requests
import hashlib
import jwt
import os
import time

from app.models.database import SessionLocal, User, get_db

router = APIRouter(tags=["Auth"])

# ---------------- CONFIG ----------------
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")  # use strong secret in .env
REFRESH_SECRET_KEY = os.getenv("REFRESH_SECRET_KEY", "your_refresh_secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

bearer = HTTPBearer()


# ---------------- TOKEN HELPERS ----------------
def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(data: dict, expires_delta: timedelta = timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, REFRESH_SECRET_KEY, algorithm=ALGORITHM)


# ---------------- AUTH MIDDLEWARE ----------------
def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
):
    if not creds:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = creds.credentials.replace("Bearer ", "")

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

        return user

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


# ---------------- UTILS ----------------
def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name,
        "email": u.email,
        "is_guest": bool(u.is_guest),
        "created_at": u.created_at,
    }


# ---------------- MODELS ----------------
class SignupPayload(BaseModel):
    name: str
    email: EmailStr
    password: str


class SigninPayload(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    token: str


class RefreshRequest(BaseModel):
    refresh: str


# ---------------- AUTH ENDPOINTS ----------------
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
            # Upgrade guest -> full user
            existing.email = payload.email
            existing.password_hash = pw_hash
            existing.is_guest = False
            db.commit()
            db.refresh(existing)
            user = existing
        else:
            # New user
            user = User(
                name=payload.name,
                email=payload.email,
                password_hash=pw_hash,
                is_guest=False,
                created_at=datetime.utcnow(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)

        access_token = create_access_token(
            data={"user_id": user.id, "email": user.email},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token({"user_id": user.id, "email": user.email})

        return {
            "user": serialize_user(user),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }
    finally:
        db.close()


@router.post("/signin")
def signin(payload: SigninPayload):
    db = SessionLocal()
    try:
        user = db.query(User).filter(
            User.email == payload.email,
            User.is_guest == False
        ).first()

        if not user or not user.password_hash:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        salt = os.getenv("AUTH_SALT", "static_salt")
        if user.password_hash != hash_password(payload.password, salt):
            raise HTTPException(status_code=401, detail="Invalid credentials")

        access_token = create_access_token(
            data={"user_id": user.id, "email": user.email},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token({"user_id": user.id, "email": user.email})

        return {
            "user": serialize_user(user),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }
    finally:
        db.close()


@router.post("/guest")
def create_guest():
    db = SessionLocal()
    try:
        user = User(email=None, password_hash=None, is_guest=True, created_at=datetime.utcnow())
        db.add(user)
        db.commit()
        db.refresh(user)

        access_token = create_access_token(
            data={"user_id": user.id, "is_guest": True},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token({"user_id": user.id, "is_guest": True})

        return {
            "user": serialize_user(user),
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
        }
    finally:
        db.close()


@router.post("/google")
def google_login(payload: GoogleAuthRequest, db: Session = Depends(get_db)):
    try:
        idinfo = id_token.verify_oauth2_token(
            payload.token,
            requests.Request(),
            GOOGLE_CLIENT_ID
        )

        email = idinfo.get("email")
        name = idinfo.get("name")
        picture = idinfo.get("picture")

        if not email:
            raise HTTPException(status_code=400, detail="Invalid Google token")

        user = db.query(User).filter(User.email == email).first()
        if not user:
            user = User(email=email, name=name, google_account=True)
            db.add(user)
            db.commit()
            db.refresh(user)

        access_token = create_access_token(
            data={"user_id": user.id, "email": email},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token({"user_id": user.id, "email": email})

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "picture": picture
            }
        }

    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------- TOKEN REFRESH ----------------
@router.post("/token/refresh")
def refresh_access_token(req: RefreshRequest):
    try:
        payload = jwt.decode(req.refresh, REFRESH_SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("user_id")
        email = payload.get("email")

        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid refresh token")

        new_access_token = create_access_token(
            data={"user_id": user_id, "email": email},
            expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        new_refresh_token = create_refresh_token({"user_id": user_id, "email": email})

        return {
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
        }

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
