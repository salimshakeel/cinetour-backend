from datetime import datetime, timedelta
import jwt
from app.config import SECRET_KEY, ALGORITHM

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload  # contains user_id, email etc.
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
