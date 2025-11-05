from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import os

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL")

def send_reset_email(to_email: str, reset_link: str):
    """
    Sends a password reset email using SendGrid.
    """
    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=to_email,
        subject="Password Reset Request",
        html_content=f"""
        <p>Hi there,</p>
        <p>We received a request to reset your password.</p>
        <p>Click the link below to reset it:</p>
        <a href="{reset_link}" target="_blank">{reset_link}</a>
        <br><br>
        <p>If you didn’t request this, just ignore this email.</p>
        """
    )

    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
        return {"message": "Password reset email sent successfully."}
    except Exception as e:
        print("Error sending email:", e)
        return {"error": str(e)}
