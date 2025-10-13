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

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
SECRET_KEY = os.getenv("SECRET_KEY", "your_secret_key")  # use strong secret in .env
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

import jwt

# Add this near the bottom of auth.py

# oauth2_scheme = OAuth2PasswordBearer(tokenUrl="signin")
bearer = HTTPBearer()

def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db)
):
    # 1) confirm we received the header
    if not creds:
        print("🛑 No credentials object received (creds is None)")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    # 2) show what the object looks like (for debugging)
    print("🟢 HTTPAuthorizationCredentials object:", repr(creds))

    # 3) the actual token string
    token = creds.credentials.replace("Bearer ", "")

    print("🟢 Token received (credentials):", token)

    try:
        # 4) decode and print payload
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        print("🟢 Decoded payload:", payload)

        user_id = payload.get("user_id")
        if not user_id:
            print("🛑 user_id missing in token payload")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            print(f"🛑 user not found for id={user_id}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

        print(f"✅ Authenticated user: {user.email} (id={user.id})")
        return user

    except jwt.ExpiredSignatureError:
        print("🛑 Token expired")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError as e:
        print("🛑 JWT decode error:", str(e))
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

def create_access_token(data: dict, expires_delta: timedelta) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + expires_delta
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


class SignupPayload(BaseModel):
    name: str
    email: EmailStr
    password: str


class SigninPayload(BaseModel):
    email: EmailStr
    password: str


def serialize_user(u: User) -> dict:
    return {
        "id": u.id,
        "name": u.name, 
        "email": u.email,
        "is_guest": bool(u.is_guest),
        "created_at": u.created_at,
    }

class GoogleAuthRequest(BaseModel):
    token: str  # ID token from frontend

def create_jwt_token(user_id: int):
    payload = {
        "sub": str(user_id),
        "exp": datetime.utcnow() + timedelta(hours=24)  # token valid for 24h
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


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
            name=payload.name, 
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
        # 🔑 only look for real users (is_guest = False)
        user = db.query(User).filter(
            User.email == payload.email,
            User.is_guest == False
        ).first()

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

        return {
            "user": serialize_user(user),
            "access_token": token,
            "token_type": "bearer"
        }
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
        
# ---------------Google-auth-------------------------------------
@router.post("/google")
def google_login(payload: GoogleAuthRequest, db: Session = Depends(get_db)):
    try:
        # ✅ Verify token with Google
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

        # ✅ Check if user exists
        user = db.query(User).filter(User.email == email).first()

        if not user:
            user = User(email=email, name=name, google_account=True)
            db.add(user)
            db.commit()
            db.refresh(user)

        # ✅ Issue JWT
        token = create_jwt_token(user.id)

        return {
            "access_token": token,
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


# ---------------FORGOT PASSWORD-------------------------------------
class ForgotPasswordRequest(BaseModel):
    email: EmailStr

class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

@router.post("/forgot-password")
def forgot_password(payload: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Send password reset email (mock implementation - integrate with email service)"""
    user = db.query(User).filter(User.email == payload.email).first()
    if not user:
        # Don't reveal if email exists for security
        return {"message": "If email exists, reset instructions have been sent"}
    
    # Generate reset token (in production, use secure random token)
    reset_token = f"reset_{user.id}_{int(time.time())}"
    
    # Store reset token in user record (add reset_token field to User model)
    # For now, we'll just return success
    # In production: send email with reset link containing token
    
    return {"message": "Password reset instructions sent to your email"}

@router.post("/reset-password")
def reset_password(payload: ResetPasswordRequest, db: Session = Depends(get_db)):
    """Reset password with token"""
    # In production, validate token and check expiration
    # For now, mock implementation
    try:
        # Extract user ID from token (mock)
        if not payload.token.startswith("reset_"):
            raise HTTPException(status_code=400, detail="Invalid reset token")
        
        # Mock token validation - in production, store and validate properly
        user_id = int(payload.token.split("_")[1])
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(status_code=400, detail="Invalid reset token")
        
        # Hash new password
        salt = os.getenv("AUTH_SALT", "static_salt")
        new_hash = hash_password(payload.new_password, salt)
        
        # Update password
        user.password_hash = new_hash
        db.commit()
        
        return {"message": "Password reset successfully"}
        
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid reset token")