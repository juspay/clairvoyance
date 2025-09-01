import boto3
import uuid
from app.core.logger import logger
from app.core.config import (
    AWS_ACCESS_KEY_ID,
    AWS_SECRET_ACCESS_KEY,
    AWS_S3_BUCKET_NAME,
)


class S3Service:
    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
        )

    def upload_file(self, file_name: str, file_content: bytes, content_type: str) -> str:
        """
        Uploads a file to S3 and returns the file name.
        """
        try:
            self.s3.put_object(
                Bucket=AWS_S3_BUCKET_NAME,
                Key=file_name,
                Body=file_content,
                ContentType=content_type,
            )
            logger.info(f"Successfully uploaded file to S3: {file_name}")
            return file_name
        except Exception as e:
            logger.error(f"Error uploading file to S3: {e}")
            raise


s3 = S3Service()
