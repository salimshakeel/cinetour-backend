# from app.models.database import SessionLocal, Video, UploadedImage, Feedback

# def debug_videos():
#     db = SessionLocal()
#     try:
#         videos = db.query(Video).all()
#         if not videos:
#             print("No videos found in the database.")
#         else:
#             print(f"Found {len(videos)} videos:")
#             for v in videos:
#                 print({
#                     "id": v.id,
#                     "\nstatus": v.status,
#                     "\nrunway_job_id": v.runway_job_id,
#                     "\nprompt": v.prompt,
#                     "\nvideo_url": v.video_url,
#                     "\nvideo_path": v.video_path,
#                     "\nimage_id": v.image_id,
#                     "\niteration": v.iteration,
#                 })

#     finally:
#         db.close()

# def debug_images():
#     db = SessionLocal()
#     try:
#         images = db.query(UploadedImage).all()
#         if not images:
#             print("No images found in the database.")
#         else:
#             print(f"Found {len(images)} images:")
#             for img in images:
#                 print({
#                     "id": img.id,
#                     "filename": img.filename,
#                     "prompt": img.prompt,
#                     "upload_time": img.upload_time,
#                 })
#     finally:
#         db.close()

# if __name__ == "__main__":
#     print("=== Videos ===")
#     debug_videos()
#     print("\n=== Images ===")
#     debug_images()

import os
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv

load_dotenv()

message = Mail(
    from_email=os.getenv("FROM_EMAIL"),
    to_emails="salimshakeel098@gmail.com",
    subject="Test Email from SendGrid",
    html_content="<strong>This is a test email from SendGrid</strong>"
)

try:
    sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
    response = sg.send(message)
    print("Status Code:", response.status_code)
    print("Body:", response.body)
    print("Headers:", response.headers)
except Exception as e:
    print("Error:", e)
