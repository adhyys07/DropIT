import http.client
import json
import os
import secrets
import shutil
import ssl

import index_db

BACKEND = os.environ.get("STORAGE_BACKEND", "local").lower()

# --- S3 / R2 / B2 / MinIO -------------------------------------------------
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "")     # blank means real AWS
S3_REGION = os.environ.get("S3_REGION", "auto")
S3_PREFIX = os.environ.get("S3_PREFIX", "blobs/")
S3_URL_TTL = int(os.environ.get("S3_URL_TTL", 900))  # presigned link lifetime

# --- Hack Club CDN --------------------------------------------------------
CDN_TOKEN = os.environ.get("CDN_TOKEN", "")
CDN_HOST = os.environ.get("CDN_HOST", "cdn.hackclub.com")
CRLF = chr(13) + chr(10)

class Backend:
    name = "base"

    # False when the backend cannot serve a stored gzip blob correctly,
    # which forces uploads to be committed verbatim.
    supports_encoding = True

    # False when the backend serves files with its own headers, so
    # Content-Disposition cannot be forced to attachment.
    supports_disposition = True

    # False when deleting a file does not reliably revoke access to it, so
    # a TTL is best-effort rather than a guarantee.
    enforces_expiry = True

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


class HackClubBackend(Backend):
    """Hack Club CDN (api/v4) as blob storage.

    Three differences from a bucket shape this:

      * No listing endpoint, so orphans cannot be detected.
      * No way to override the response Content-Disposition or
        Content-Encoding, so files are served however the CDN decides and
        must be stored verbatim.
      * URLs are permanent and public until explicitly deleted, so the
        expiry reaper is the only thing between an expired file and a link
        that still works.

    The CDN addresses files by its own upload id rather than by our digest,
    so that id is kept on the blob row and is what delete() needs.
    """

    name = "hackclub"
    supports_encoding = False
    supports_disposition = False

    # Measured, not assumed: files are served with
    # `Cache-Control: public, max-age=2592000`, and after a successful
    # DELETE the Cloudflare edge keeps serving CF-Cache-Status: HIT. A file
    # that was downloaded once stays reachable for up to 30 days past its
    # expiry, and there is no purge API available to us.
    enforces_expiry = False

    def __init__(self, token: str = "", host: str = "", conn_factory=None):
        self.token = token or CDN_TOKEN
        self.host = host or CDN_HOST
        self._conn_factory = conn_factory          # injected for tests
        if not self.token:
            raise RuntimeError(
                "STORAGE_BACKEND=hackclub needs CDN_TOKEN. Create an API key "
                "at https://cdn.hackclub.com after signing in."
            )

    def _connect(self):
        if self._conn_factory is not None:
            return self._conn_factory()
        return http.client.HTTPSConnection(
            self.host, timeout=600, context=ssl.create_default_context())

    def _request(self, method, path, body=None, stream_path="", stream_len=0,
                 boundary=""):
        """One HTTPS call. A file is streamed from disk, never buffered."""
        conn = self._connect()
        try:
            conn.putrequest(method, path, skip_accept_encoding=True)
            conn.putheader("Authorization", "Bearer " + self.token)
            if stream_path:
                conn.putheader("Content-Type",
                               "multipart/form-data; boundary=" + boundary)
                conn.putheader("Content-Length", str(stream_len))
                conn.endheaders()
                with open(stream_path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 256)
                        if not chunk:
                            break
                        conn.send(chunk)
            elif body is not None:
                raw = json.dumps(body).encode()
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(raw)))
                conn.endheaders()
                conn.send(raw)
            else:
                conn.endheaders()
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def exists(self, digest: str) -> bool:
        # There is no HEAD-by-digest, so our own record is the answer.
        return bool(index_db.get_remote(digest))

    def _envelope(self, digest: str, boundary: str) -> tuple[bytes, bytes]:
        head = (
            "--" + boundary + CRLF
            + 'Content-Disposition: form-data; name="file"; filename="'
            + digest[:16] + '.bin"' + CRLF
            + "Content-Type: application/octet-stream" + CRLF + CRLF
        ).encode()
        tail = (CRLF + "--" + boundary + "--" + CRLF).encode()
        return head, tail

    def commit(self, digest: str, temp_path: str, enc: str = "") -> bool:
        if enc:
            raise RuntimeError(
                "The Hack Club CDN cannot serve a gzip-encoded blob; "
                "uploads must be committed verbatim."
            )
        staged = temp_path + ".mp"
        try:
            if self.exists(digest):
                return False

            boundary = "----dropit" + secrets.token_hex(16)
            head, tail = self._envelope(digest, boundary)
            size = os.path.getsize(temp_path)

            # Assembling the envelope on disk keeps Content-Length exact and
            # memory flat for a multi-gigabyte upload.
            with open(staged, "wb") as out, open(temp_path, "rb") as src:
                out.write(head)
                shutil.copyfileobj(src, out, 1024 * 256)
                out.write(tail)

            status, payload = self._request(
                "POST", "/api/v4/upload", stream_path=staged,
                stream_len=len(head) + size + len(tail), boundary=boundary)

            if status not in (200, 201):
                raise RuntimeError(
                    "CDN upload failed (" + str(status) + "): "
                    + payload[:300].decode("utf-8", "replace"))
            info = json.loads(payload)
            if not info.get("id") or not info.get("url"):
                raise RuntimeError(
                    "CDN upload returned no id or url: "
                    + payload[:200].decode("utf-8", "replace"))
            index_db.set_remote(digest, info["id"], info["url"])
            return True
        finally:
            for path in (staged, temp_path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def delete(self, digest: str) -> None:
        remote = index_db.get_remote(digest)
        if not remote:
            return
        try:
            status, payload = self._request(
                "DELETE", "/api/v4/upload/" + remote["id"])
            if status not in (200, 202, 204, 404):
                # Logged on purpose: the record is dropped either way, so a
                # silent failure here leaks a file nothing will retry.
                print("[cdn] delete of " + remote["id"] + " failed ("
                      + str(status) + "): "
                      + payload[:200].decode("utf-8", "replace"), flush=True)
        except Exception as err:
            print("[cdn] delete of " + remote["id"] + " errored: "
                  + str(err), flush=True)
        # The record goes either way: keeping it would make the blob
        # undeletable, and the CDN charges the quota regardless.
        index_db.clear_remote(digest)

    def local_path(self, digest: str) -> str | None:
        return None

    def presigned_url(self, digest: str, filename: str,
                      content_type: str, inline: bool) -> str | None:
        """The CDN's permanent URL.

        Note what this cannot do: the CDN decides the response content type
        and disposition, so filename, content_type and inline are ignored
        and a download is not forced. See supports_disposition.
        """
        remote = index_db.get_remote(digest)
        return remote["url"] if remote else None

    def list_digests(self) -> set[str]:
        """Unsupported: api/v4 has no listing endpoint.

        Empty is the safe answer. sweep_orphans() computes stored minus
        known, so an empty set finds nothing rather than proposing to
        delete every blob.
        """
        return set()

    def quota(self) -> dict | None:
        """Key owner and storage ceiling, straight from the CDN."""
        try:
            status, payload = self._request("GET", "/api/v4/me")
            return json.loads(payload) if status == 200 else None
        except Exception:
            return None


def make(blob_dir: str) -> Backend:
    if BACKEND == "s3":
        return S3Backend(S3_BUCKET, S3_ENDPOINT, S3_REGION, S3_PREFIX)
    if BACKEND in ("hackclub", "cdn"):
        return HackClubBackend()
    return LocalBackend(blob_dir)
