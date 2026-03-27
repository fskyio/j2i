from __future__ import annotations

import asyncio
import logging
import urllib.request

log = logging.getLogger(__name__)

# Known pastebin services and their form field names
_KNOWN_SERVICES: dict[str, tuple[str, str]] = {
    # name -> (url, form_field)
    "txt.t0.vc": ("https://txt.t0.vc", "txt"),
    "kmi.aeza.net": ("https://kmi.aeza.net", "kmi"),
}


async def upload(
    service: str,
    text: str,
    auth: str | None = None,
    field_override: str | None = None,
) -> str | None:
    """Upload text to a pastebin service and return the URL.

    Args:
        service: Service name (e.g. "txt.t0.vc") or custom URL.
        text: The text to upload.
        auth: Optional Authorization header value (e.g. "Bearer token123").
        field_override: Override the form field name for custom pastebins.
    """
    return await asyncio.to_thread(
        _upload_sync, service, text, auth, field_override
    )


def _upload_sync(
    service: str,
    text: str,
    auth: str | None = None,
    field_override: str | None = None,
) -> str | None:
    if service in _KNOWN_SERVICES:
        url, field_name = _KNOWN_SERVICES[service]
    else:
        url = service if service.startswith("http") else f"https://{service}"
        field_name = "txt"

    if field_override:
        field_name = field_override

    try:
        boundary = "----j2iBoundary"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"\r\n'
            f"\r\n"
            f"{text}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        if auth:
            headers["Authorization"] = auth

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = resp.read().decode("utf-8").strip()
            log.debug("Pastebin response: %s", result)
            return result
    except Exception:
        log.exception("Failed to upload to pastebin %s", service)
        return None
