import shutil
from uuid import uuid4
from fastapi import UploadFile
from app.config import UPLOAD_DIR
import os

def save_upload_file(upload_file: UploadFile) -> str:
    file_ext = os.path.splitext(upload_file.filename)[1]
    unique_filename = f"{uuid4().hex}{file_ext}"
    file_path = os.path.join(UPLOAD_DIR, unique_filename)
    
    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(upload_file.file, buffer)
    
    return unique_filename
