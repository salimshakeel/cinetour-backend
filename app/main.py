from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import upload, Client, admin, auth, stripe, admin_auth  # 👈 added admin_auth
from app.config import RUNWAY_API_KEY
from dotenv import load_dotenv
import os
from starlette.staticfiles import StaticFiles
from app.models.database import Base, engine

app = FastAPI(
    title="Real Estate Video Backend",
    version="1.0.0",
)

# Create all DB tables
Base.metadata.create_all(bind=engine)

# Routers
app.include_router(upload.router)
app.include_router(Client.router, prefix="/api/client")
app.include_router(admin.router, prefix="/api")
app.include_router(admin_auth.router, prefix="/api/admin/auth")
app.include_router(auth.router, prefix="/auth")
app.include_router(stripe.router, prefix="/stripe")

# Static file mounts
os.makedirs("videos", exist_ok=True)
app.mount("/videos", StaticFiles(directory="videos"), name="videos")

os.makedirs("uploaded_images", exist_ok=True)
app.mount("/uploaded_images", StaticFiles(directory="uploaded_images"), name="uploaded_images")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://cinetours.vercel.app",    # frontend
        "https://qunatum-tour.onrender.com",  # backend
        "http://localhost:3000",           # local dev
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Load env
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
print("Loaded API key:", api_key)

def print_openai_credentials():
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    project_id = os.getenv("OPENAI_PROJECT", "").strip()

    if not api_key or not project_id:
        print("❌ OpenAI credentials missing! Set OPENAI_API_KEY and OPENAI_PROJECT.")
    else:
        print("✅ Loaded OpenAI credentials:")
        print(f"API key: {api_key[:8]}...{api_key[-8:]}")
        print(f"Project ID: {project_id}")

print_openai_credentials()
