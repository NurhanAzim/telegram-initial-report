from __future__ import annotations

import mimetypes
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import requests


@dataclass(slots=True)
class ShareInfo:
    remote_path: str
    share_id: str | None
    share_url: str


class NextcloudClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        upload_dir: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.upload_dir = self._normalize_dir(upload_dir)
        self.auth = (username, password)
        self.share_headers = {"OCS-APIRequest": "true"}

    def upload_and_share(self, local_path: Path, remote_name: str | None = None) -> ShareInfo:
        remote_name = remote_name or local_path.name
        remote_path = self.upload_dir / remote_name
        self._ensure_directory(self.upload_dir)
        self._upload_file(local_path, remote_path)
        return self._create_public_share(remote_path)

    def delete_share(self, share_id: str) -> None:
        response = requests.delete(
            f"{self.base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares/{quote(share_id, safe='')}",
            auth=self.auth,
            headers=self.share_headers,
            timeout=60,
        )
        if response.status_code not in {200, 404}:
            response.raise_for_status()

    def delete_file(self, remote_path: str | PurePosixPath) -> None:
        path = PurePosixPath(remote_path) if isinstance(remote_path, str) else remote_path
        response = requests.delete(
            self._dav_url(path),
            auth=self.auth,
            timeout=60,
        )
        if response.status_code not in {204, 404}:
            response.raise_for_status()

    def _normalize_dir(self, upload_dir: str) -> PurePosixPath:
        cleaned = upload_dir.strip().strip("/")
        return PurePosixPath(cleaned) if cleaned else PurePosixPath()

    def _dav_url(self, remote_path: PurePosixPath) -> str:
        base = f"{self.base_url}/remote.php/dav/files/{quote(self.username, safe='')}"
        if not remote_path.parts:
            return base
        path = "/".join(quote(part, safe="") for part in remote_path.parts)
        return f"{base}/{path}"

    def _ensure_directory(self, remote_dir: PurePosixPath) -> None:
        current = PurePosixPath()
        for part in remote_dir.parts:
            current /= part
            response = requests.request(
                "MKCOL",
                self._dav_url(current),
                auth=self.auth,
                timeout=60,
            )
            if response.status_code not in {201, 405}:
                response.raise_for_status()

    def _upload_file(self, local_path: Path, remote_path: PurePosixPath) -> None:
        content_type, _ = mimetypes.guess_type(str(local_path))
        with local_path.open("rb") as handle:
            response = requests.put(
                self._dav_url(remote_path),
                auth=self.auth,
                data=handle,
                headers={"Content-Type": content_type or "application/octet-stream"},
                timeout=120,
            )
        response.raise_for_status()

    def _create_public_share(self, remote_path: PurePosixPath) -> ShareInfo:
        response = requests.post(
            f"{self.base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares",
            auth=self.auth,
            headers=self.share_headers,
            data={
                "path": self._ocs_path(remote_path),
                "shareType": "3",
                "permissions": "1",
            },
            timeout=60,
        )
        response.raise_for_status()
        share_id, share_url = self._extract_share_info(response.text)
        return ShareInfo(
            remote_path=self._ocs_path(remote_path).lstrip("/"),
            share_id=share_id,
            share_url=share_url,
        )

    def _ocs_path(self, remote_path: PurePosixPath) -> str:
        if not remote_path.parts:
            return "/"
        return "/" + "/".join(remote_path.parts)

    def _extract_share_info(self, body: str) -> tuple[str | None, str]:
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise RuntimeError("Nextcloud share response was not valid XML.") from exc

        status = self._find_text(root, "status").lower()
        status_code = self._find_text(root, "statuscode")
        if not self._is_success(status, status_code):
            message = self._find_text(root, "message") or "Unknown Nextcloud share error."
            raise RuntimeError(f"Nextcloud share creation failed: {message}")

        url = self._find_text(root, "url") or self._find_text(root, "link")
        if not url:
            raise RuntimeError("Nextcloud share response did not include a public URL.")
        share_id = self._find_text(root, "id") or None
        return share_id, url.strip()

    def _find_text(self, root: ET.Element, local_name: str) -> str:
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == local_name and element.text:
                return element.text.strip()
        return ""

    def _is_success(self, status: str, status_code: str) -> bool:
        if status == "ok":
            return True
        return status_code.strip() in {"100", "200", "201"}


def sanitize_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "report"
