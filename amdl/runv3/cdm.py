"""Pure-Python Widevine CDM.

Port of utils/runv3/cdm/cdm.go: builds signed license requests and extracts
content keys from license responses using an embedded RSA device key.
"""

from __future__ import annotations

import os
import time

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .proto_codec import (
    Field,
    data_field,
    encode_fields,
    get_fields,
    get_varints,
    msg_field,
    parse_fields,
    raw_field,
    vint_field,
)

# --- protocol constants (values from wv_proto2.pb.go) --------------------------

SIGNED_LICENSE_REQUEST_LICENSE_REQUEST = 1  # SignedLicenseRequest_MessageType
LICENSE_REQUEST_NEW = 1  # LicenseRequest_RequestType
LICENSE_TYPE_DEFAULT = 1  # LicenseType (STREAMING)
PROTOCOL_VERSION_CURRENT = 21
WIDEVINE_CENC_HEADER_AESCTR = 1  # WidevineCencHeader_Algorithm
LICENSE_KEY_CONTAINER_CONTENT = 2  # License_KeyContainer_KeyType

# Field numbers mirrored from wv_proto2.pb.go tags.
F_SLR_TYPE = 1
F_SLR_MSG = 2
F_SLR_SIGNATURE = 3

F_LR_CLIENT_ID = 1
F_LR_CONTENT_ID = 2
F_LR_TYPE = 3
F_LR_REQUEST_TIME = 4
F_LR_PROTOCOL_VERSION = 6
F_LR_KEY_CONTROL_NONCE = 7
F_LR_ENCRYPTED_CLIENT_ID = 8

F_CI_CENC_ID = 1
F_CENC_PSSH = 1
F_CENC_LICENSE_TYPE = 2
F_CENC_REQUEST_ID = 3

F_SL_SESSION_KEY = 4
F_LICENSE_KEY = 3

F_KC_ID = 1
F_KC_IV = 2
F_KC_KEY = 3
F_KC_TYPE = 4


class Key:
    def __init__(self, key_id: bytes, key_type: int, value: bytes) -> None:
        self.id = key_id
        self.type = key_type
        self.value = value


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    count = data[-1]
    if count < 1 or count > 16:
        return data
    return data[:-count]


def aes_cmac(key: bytes, message: bytes) -> bytes:
    """AES-CMAC (RFC 4493) built on AES-ECB."""

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    def encrypt_block(block: bytes) -> bytes:
        enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        return enc.update(block) + enc.finalize()

    def dbl(block: bytes) -> bytes:
        i = int.from_bytes(block, "big")
        i <<= 1
        if block[0] & 0x80:
            i ^= 0x87
        return (i & ((1 << 128) - 1)).to_bytes(16, "big")

    subkey1 = dbl(encrypt_block(b"\x00" * 16))
    subkey2 = dbl(subkey1)

    n_blocks = (len(message) + 15) // 16
    if n_blocks == 0:
        blocks = [b"\x80" + b"\x00" * 15]
        n_blocks = 1
    else:
        blocks = [message[i * 16 : (i + 1) * 16] for i in range(n_blocks)]
    last = blocks[-1]
    if len(last) == 16:
        last = bytes(a ^ b for a, b in zip(last, subkey1))
    else:
        pad_len = 16 - len(last)
        last = last + b"\x80" + b"\x00" * (pad_len - 1)
        last = bytes(a ^ b for a, b in zip(last, subkey2))
    x = b"\x00" * 16
    for block in blocks[:-1]:
        x = encrypt_block(bytes(a ^ b for a, b in zip(x, block)))
    return encrypt_block(bytes(a ^ b for a, b in zip(x, last)))


class CDM:
    """One license session bound to a PSSH (Go type wv.CDM)."""

    def __init__(self, private_key_pem: str, client_id: bytes, init_data: bytes) -> None:
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(), password=None
        )
        self.client_id = client_id
        if len(init_data) < 32:
            raise ValueError("initData not long enough")
        # Validate that the payload after the 32-byte prefix parses as a
        # WidevineCencHeader (any schema-free garbage raises here).
        parse_fields(init_data[32:])
        self.widevine_cenc_header_raw = init_data[32:]
        self.privacy_mode = False

        charset = "ABCDEF0123456789"
        s = bytearray(32)
        for i in range(16):
            s[i] = ord(charset[os.urandom(1)[0] % len(charset)])
        s[16] = ord("0")
        s[17] = ord("1")
        for i in range(18, 32):
            s[i] = ord("0")
        self.session_id = bytes(s)

    @classmethod
    def new_default(cls, init_data: bytes) -> "CDM":
        import base64

        from .device_consts import DEFAULT_CLIENT_ID_B64, DEFAULT_PRIVATE_KEY_PEM

        client_id = base64.b64decode(DEFAULT_CLIENT_ID_B64)
        return cls(DEFAULT_PRIVATE_KEY_PEM, client_id, init_data)

    def get_license_request(self) -> bytes:
        """Build and sign the SignedLicenseRequest message."""
        nonce = int.from_bytes(os.urandom(4), "big")

        cenc = [
            raw_field(F_CENC_PSSH, self.widevine_cenc_header_raw),
            vint_field(F_CENC_LICENSE_TYPE, LICENSE_TYPE_DEFAULT),
            data_field(F_CENC_REQUEST_ID, self.session_id),
        ]
        content_id = [msg_field(F_CI_CENC_ID, cenc)]

        license_request = [
            # ClientIdentification blob is embedded verbatim.
            raw_field(F_LR_CLIENT_ID, self.client_id),
            msg_field(F_LR_CONTENT_ID, content_id),
            vint_field(F_LR_TYPE, LICENSE_REQUEST_NEW),
            vint_field(F_LR_REQUEST_TIME, int(time.time())),
            vint_field(F_LR_PROTOCOL_VERSION, PROTOCOL_VERSION_CURRENT),
            vint_field(F_LR_KEY_CONTROL_NONCE, nonce),
        ]
        request_msg = encode_fields(license_request)

        digest = hashes.Hash(hashes.SHA1())
        digest.update(request_msg)
        signature = self.private_key.sign(
            digest.finalize(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA1()), salt_length=20),
            hashes.SHA1(),
        )

        signed_request: list[Field] = [
            vint_field(F_SLR_TYPE, SIGNED_LICENSE_REQUEST_LICENSE_REQUEST),
            raw_field(F_SLR_MSG, request_msg),
            data_field(F_SLR_SIGNATURE, signature),
        ]
        return encode_fields(signed_request)

    def get_license_keys(
        self, license_request_bytes: bytes, license_response: bytes
    ) -> list[Key]:
        # Recover the inner LicenseRequest we marshaled during signing; Apple
        # computes its key derivation over exactly these bytes.
        inner_msgs = get_fields(license_request_bytes, F_SLR_MSG)
        if not inner_msgs:
            raise ValueError("license request has no Msg field")
        license_request_msg = inner_msgs[0]

        sl_fields = parse_fields(license_response)
        wrapped_session_key = None
        license_msgs = []
        for field_no, wire_type, value in sl_fields:
            if field_no == F_SL_SESSION_KEY and wire_type == 2:
                wrapped_session_key = value
            elif field_no == F_SLR_MSG and wire_type == 2:
                license_msgs.append(value)
        if wrapped_session_key is None or not license_msgs:
            raise ValueError("license response missing SessionKey or Msg")
        license_msg = license_msgs[0]

        session_key = self.private_key.decrypt(
            wrapped_session_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA1()),
                algorithm=hashes.SHA1(),
                label=None,
            ),
        )

        encryption_key_input = (
            b"\x01ENCRYPTION\x00" + license_request_msg + b"\x00\x00\x00\x80"
        )
        cmac_key = aes_cmac(session_key, encryption_key_input)

        keys: list[Key] = []
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        for container in get_fields(license_msg, F_LICENSE_KEY):
            container_fields = parse_fields(container)
            iv: bytes | None = None
            key_data: bytes | None = None
            key_id: bytes = b""
            key_type: int | None = None
            for field_no, wire_type, value in container_fields:
                if field_no == F_KC_IV and wire_type == 2:
                    iv = value
                elif field_no == F_KC_KEY and wire_type == 2:
                    key_data = value
                elif field_no == F_KC_ID and wire_type == 2:
                    key_id = value
                elif field_no == F_KC_TYPE and wire_type == 0:
                    key_type = value
            if iv is None or key_data is None:
                continue
            dec = Cipher(algorithms.AES(cmac_key), modes.CBC(iv)).decryptor()
            decrypted = dec.update(key_data) + dec.finalize()
            keys.append(Key(key_id, key_type or 0, _pkcs7_unpad(decrypted)))
        return keys
