"""
Email service for sending generated images via SMTP.
"""

import asyncio
import os
import smtplib
import ssl
import tempfile
from datetime import datetime
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import aiohttp

from app.core import config
from app.core.logger import logger


class EmailService:
    """Service for sending emails with image attachments via SMTP"""

    def __init__(self):
        self.smtp_server = config.SMTP_SERVER
        self.smtp_port = config.SMTP_PORT
        self.username = config.SMTP_USERNAME
        self.password = config.SMTP_PASSWORD
        self.from_email = config.SMTP_FROM_EMAIL
        self.to_email = config.SMTP_TO_EMAIL
        self.use_tls = config.SMTP_USE_TLS

    async def send_images_email(
        self,
        images: List[Dict[str, Any]],
        subject: str = None,
        custom_message: str = None,
    ) -> bool:
        """
        Send email with image attachments.

        Args:
            images: List of image metadata dicts with URLs
            subject: Optional custom subject line
            custom_message: Optional custom message body

        Returns:
            bool: True if email sent successfully, False otherwise
        """
        try:
            if not images:
                logger.warning("EmailService: No images provided for email")
                return False

            # Create email message
            msg = MIMEMultipart()
            msg["From"] = self.from_email
            msg["To"] = self.to_email
            msg["Subject"] = (
                subject
                or f"Generated Images from Clairvoyance - {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            )

            # Create email body
            body = await self._create_email_body(images, custom_message)
            msg.attach(MIMEText(body, "html"))

            # Download and attach images
            attached_count = 0
            for i, image in enumerate(images):
                try:
                    image_data = await self._download_image(image)
                    if image_data:
                        # Create image attachment
                        img_attachment = MIMEImage(image_data)
                        filename = f"image_{i+1}.png"
                        img_attachment.add_header(
                            "Content-Disposition", f'attachment; filename="{filename}"'
                        )
                        msg.attach(img_attachment)
                        attached_count += 1
                        logger.info(f"EmailService: Attached image {i+1}: {filename}")
                    else:
                        logger.warning(f"EmailService: Failed to download image {i+1}")
                except Exception as e:
                    logger.error(f"EmailService: Error attaching image {i+1}: {e}")

            if attached_count == 0:
                logger.error("EmailService: No images could be attached")
                return False

            # Send email via SMTP
            success = await self._send_smtp_email(msg)

            if success:
                logger.info(
                    f"EmailService: Successfully sent email with {attached_count} image(s) to {self.to_email}"
                )
            else:
                logger.error("EmailService: Failed to send email via SMTP")

            return success

        except Exception as e:
            logger.error(f"EmailService: Error sending images email: {e}")
            return False

    async def _create_email_body(
        self, images: List[Dict[str, Any]], custom_message: str = None
    ) -> str:
        """Create HTML email body with image descriptions"""
        try:
            body_parts = []

            if custom_message:
                body_parts.append(f"<p>{custom_message}</p>")
            else:
                body_parts.append(
                    "<p>Here are your generated images from Clairvoyance:</p>"
                )

            body_parts.append("<ul>")

            for i, image in enumerate(images):
                title = image.get("nav_title", image.get("title", f"Image {i+1}"))
                description = image.get("nav_description", image.get("description", ""))
                operation = image.get(
                    "nav_operation", image.get("operation", "generated")
                )
                created_at = image.get("created_at", "")

                body_parts.append(f"<li><strong>{title}</strong>")
                if description:
                    body_parts.append(f" - {description}")
                if operation:
                    body_parts.append(f" ({operation})")
                if created_at:
                    body_parts.append(f" <em>Created: {created_at}</em>")
                body_parts.append("</li>")

            body_parts.append("</ul>")
            body_parts.append("<p><em>Generated by Clairvoyance Voice Agent</em></p>")

            return "\n".join(body_parts)

        except Exception as e:
            logger.error(f"EmailService: Error creating email body: {e}")
            return "<p>Generated images attached.</p>"

    async def _download_image(self, image: Dict[str, Any]) -> Optional[bytes]:
        """Download image from URL or read from local file system"""
        try:
            # Get image URL
            image_url = (
                image.get("url")
                or image.get("imageUrl")
                or image.get("props", {}).get("imageUrl")
            )

            if not image_url:
                logger.warning(f"EmailService: No URL found for image: {image}")
                return None

            # Handle local file paths (starting with /static/)
            if image_url.startswith("/static/"):
                # Try to read directly from file system first
                try:
                    # Convert URL path to file system path
                    file_path = image_url[1:]  # Remove leading slash
                    full_path = os.path.join(os.getcwd(), file_path)

                    if os.path.exists(full_path):
                        logger.info(
                            f"EmailService: Reading image from local file: {full_path}"
                        )

                        def read_file():
                            with open(full_path, "rb") as f:
                                return f.read()

                        image_data = await asyncio.get_event_loop().run_in_executor(
                            None, read_file
                        )
                        logger.info(
                            f"EmailService: Successfully read local image ({len(image_data)} bytes)"
                        )
                        return image_data
                    else:
                        logger.warning(
                            f"EmailService: Local file not found: {full_path}"
                        )
                except Exception as file_error:
                    logger.warning(
                        f"EmailService: Error reading local file: {file_error}"
                    )

            # Fall back to HTTP download
            # Handle relative URLs by making them absolute
            if image_url.startswith("/"):
                base_url = config.APP_BASE_URL
                if not base_url:
                    # Construct base URL from HOST and PORT
                    host = getattr(config, "HOST", "localhost")
                    port = getattr(config, "PORT", 8000)
                    if host == "0.0.0.0":
                        host = "localhost"  # Use localhost for external access
                    base_url = f"http://{host}:{port}"

                image_url = f"{base_url}{image_url}"

            logger.info(f"EmailService: Downloading image from URL: {image_url}")

            # Download image data via HTTP
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    image_url, timeout=aiohttp.ClientTimeout(total=30)
                ) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        logger.info(
                            f"EmailService: Successfully downloaded image ({len(image_data)} bytes)"
                        )
                        return image_data
                    else:
                        logger.error(
                            f"EmailService: Failed to download image. Status: {response.status}"
                        )
                        return None

        except Exception as e:
            logger.error(f"EmailService: Error downloading image: {e}")
            return None

    async def _send_smtp_email(self, msg: MIMEMultipart) -> bool:
        """Send email via SMTP using synchronous smtplib in async wrapper"""
        try:
            # Run the synchronous SMTP code in a thread
            def send_email_sync():
                # Create a secure SSL/TLS context
                context = ssl.create_default_context()

                # Gmail SMTP with STARTTLS
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls(context=context)  # Enable security
                    server.login(self.username, self.password)
                    server.send_message(msg)

            # Run in thread pool to avoid blocking
            await asyncio.get_event_loop().run_in_executor(None, send_email_sync)

            logger.info("EmailService: SMTP email sent successfully")
            return True

        except Exception as e:
            logger.error(f"EmailService: SMTP error: {e}")
            return False

    async def test_connection(self) -> bool:
        """Test SMTP connection and authentication"""
        try:
            # Run the synchronous SMTP code in a thread
            def test_connection_sync():
                # Create a secure SSL/TLS context
                context = ssl.create_default_context()

                # Gmail SMTP with STARTTLS
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls(context=context)  # Enable security
                    server.login(self.username, self.password)

            # Run in thread pool to avoid blocking
            await asyncio.get_event_loop().run_in_executor(None, test_connection_sync)

            logger.info("EmailService: SMTP connection test successful")
            return True

        except Exception as e:
            logger.error(f"EmailService: SMTP connection test failed: {e}")
            return False


# Global email service instance
_email_service = None


def get_email_service() -> EmailService:
    """Get singleton email service instance"""
    global _email_service
    if _email_service is None:
        _email_service = EmailService()
    return _email_service
