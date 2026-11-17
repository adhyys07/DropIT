import os
import shutil

BACKEND = os.environ.get("STORAGE_BACKEND", "local").lower()

S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")          # R2/B2/MinIO; blank = AWS
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_PREFIX = os.environ.get("S3_PREFIX", "blobs/")
S3_URL_TTL = int(os.environ.get("S3_URL_TTL", 900))      


class Backend:
    name = "base"

    def exists(self, digest: str) -> bool:
        raise NotImplementedError

    def commit(self, digest: str, temp_path: str, enc: str = "") -> bool:
        raise NotImplementedError


    def delete(self, digest: str) -> None:
        raise NotImplementedError

    def local_path(self, digest: str) -> str | None:
        return None

    def presigned_url(self, digest: str, filename: str,
                      content_type: str, inline: bool) -> str | None:
        return None

    def list_digests(self) -> set[str]:
        raise NotImplementedError


class LocalBackend(Backend):
    name = "local"

    def __init__(self, blob_dir: str):
        self.blob_dir = blob_dir
        os.makedirs(blob_dir, exist_ok=True)

    def _path(self, digest: str) -> str:
        return os.path.join(self.blob_dir, digest[:2], digest)

    def exists(self, digest: str) -> bool:
        return os.path.exists(self._path(digest))

    def commit(self, digest: str, temp_path: str, enc: str = "") -> bool:
        dest = self._path(digest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(temp_path)
            return False
        shutil.move(temp_path, dest)
        return True


    def delete(self, digest: str) -> None:
        try:
            os.remove(self._path(digest))
        except OSError:
            pass

    def local_path(self, digest: str) -> str | None:
        p = self._path(digest)
        return p if os.path.exists(p) else None

    def list_digests(self) -> set[str]:
        out = set()
        for root, _dirs, files in os.walk(self.blob_dir):
            for f in files:
                if not f.startswith(".") and len(f) == 64:
                    out.add(f)
        return out


class S3Backend(Backend):
    name = "s3"

    def __init__(self, bucket: str, endpoint: str = "", region: str = "auto",
                 prefix: str = "blobs/", client=None):
        if not bucket:
            raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is not set.")
        self.bucket = bucket
        self.prefix = prefix
        if client is not None:
            self.s3 = client                     
        else:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as err:
                raise RuntimeError(
                    "STORAGE_BACKEND=s3 needs boto3: pip install boto3"
                ) from err
            kwargs = {
                "region_name": region,
                "config": Config(signature_version="s3v4",
                                 retries={"max_attempts": 3, "mode": "standard"}),
            }
            if endpoint:
                kwargs["endpoint_url"] = endpoint
            self.s3 = boto3.client("s3", **kwargs)

    def _key(self, digest: str) -> str:
        return f"{self.prefix}{digest[:2]}/{digest}"

    def exists(self, digest: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._key(digest))
            return True
        except Exception:
            return False

    def commit(self, digest: str, temp_path: str, enc: str = "") -> bool:
        key = self._key(digest)
        stored = False
        try:
            if not self.exists(digest):
                extra = {"ContentType": "application/octet-stream"}
                if enc:
                    extra["ContentEncoding"] = enc
                self.s3.upload_file(temp_path, self.bucket, key, ExtraArgs=extra)
                stored = True
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return stored


    def delete(self, digest: str) -> None:
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=self._key(digest))
        except Exception:
            pass

    def presigned_url(self, digest: str, filename: str,
                      content_type: str, inline: bool) -> str | None:
        safe = filename.replace('"', "")
        disposition = f'{"inline" if inline else "attachment"}; filename="{safe}"'
        try:
            return self.s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": self.bucket,
                    "Key": self._key(digest),
                    "ResponseContentDisposition": disposition,
                    "ResponseContentType": content_type,
                },
                ExpiresIn=S3_URL_TTL,
            )
        except Exception:
            return None

    def list_digests(self) -> set[str]:
        out = set()
        try:
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
                for obj in page.get("Contents", []):
                    name = obj["Key"].rsplit("/", 1)[-1]
                    if len(name) == 64:
                        out.add(name)
        except Exception:
            pass
        return out


def make(blob_dir: str) -> Backend:
    if BACKEND == "s3":
        return S3Backend(S3_BUCKET, S3_ENDPOINT, S3_REGION, S3_PREFIX)
    return LocalBackend(blob_dir)
