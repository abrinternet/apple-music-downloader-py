"""License-key acquisition.

Port of utils/runv3/key/key.go.
"""

from __future__ import annotations

import httpx

from .cdm import LICENSE_KEY_CONTAINER_CONTENT, CDM


class KeyFetcher:
    """One-shot license fetcher (Go type wv.Key)."""

    def __init__(
        self,
        client: httpx.Client,
        before_request=None,
        after_request=None,
    ) -> None:
        self.client = client
        self.before_request = before_request
        self.after_request = after_request

    def cdm_init(self) -> None:
        # The Go implementation lazily loads its device constants here;
        # Python constants are module-level already.
        return None

    def get_key(
        self,
        ctx: dict,
        license_server_url: str,
        pssh: str,
        headers: dict | None = None,
    ) -> tuple[str, bytes]:
        """Returns (hex-content-key, raw content key)."""
        import base64

        init_data = base64.b64decode(pssh)
        cdm = CDM.new_default(init_data)
        license_request = cdm.get_license_request()

        if self.before_request is not None:
            response = self.before_request(self.client, ctx, license_server_url, license_request)
            if isinstance(response, httpx.Response):
                license_response_bytes = response.content
            else:
                license_response_bytes = response
        else:
            response = self.client.post(
                license_server_url,
                content=license_request,
                headers=headers or {},
            )
            license_response_bytes = response.content

        if self.after_request is not None:
            license_response_bytes = self.after_request(license_response_bytes)

        keys = cdm.get_license_keys(license_request, license_response_bytes)

        command = ""
        key_bytes = b""
        for key in keys:
            if key.type == LICENSE_KEY_CONTAINER_CONTENT:
                command += key.value.hex()
                key_bytes = key.value
        return command, key_bytes
