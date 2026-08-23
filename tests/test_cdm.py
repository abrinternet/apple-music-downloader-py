"""CDM self-consistency: sign -> verify, then act as the license server."""

import base64
import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from amdl.runv3.cdm import LICENSE_KEY_CONTAINER_CONTENT, CDM, aes_cmac
from amdl.runv3.device_consts import DEFAULT_CLIENT_ID_B64
from amdl.runv3.proto_codec import (
    data_field,
    encode_fields,
    get_fields,
    msg_field,
    parse_fields,
    raw_field,
    vint_field,
)


def _widevine_cenc_header(kid: bytes) -> bytes:
    return encode_fields(
        [
            vint_field(1, 1),  # algorithm AESCTR
            data_field(2, kid),
            data_field(3, ""),  # provider
            data_field(4, b""),  # content_id
            data_field(6, ""),  # policy
        ]
    )


def _pssh_box_v0(payload_fields: bytes) -> bytes:
    import struct

    data = payload_fields
    return (
        struct.pack(">I", 32 + len(data))
        + b"pssh"
        + b"\x00\x00\x00\x00"
        + b"\x12" * 16
        + struct.pack(">I", len(data))
        + data
    )


def test_get_pssh_paths():
    import base64
    import struct

    from amdl.runv3.runner import get_pssh

    kid = bytes(range(16))

    # Raw-KID input gets wrapped into a prefixed WidevineCencHeader blob.
    pssh = get_pssh("", base64.b64encode(kid).decode())
    raw = base64.b64decode(pssh)
    assert raw[:32] == b"0123456789abcdef0123456789abcdef"
    fields = parse_fields(raw[32:])
    assert [v for no, _wt, v in fields if no == 2][0] == kid

    # A complete PSSH box passes through unchanged.
    data = encode_fields([vint_field(1, 1), data_field(2, kid)])
    box = (
        struct.pack(">I", 32 + len(data))
        + b"pssh"
        + b"\x00\x00\x00\x00"
        + b"\x12" * 16
        + struct.pack(">I", len(data))
        + data
    )
    blob = base64.b64encode(box).decode()
    assert get_pssh("", blob) == blob


def test_request_signature_and_key_roundtrip():
    kid = bytes(range(16))
    init_data = b"0123456789abcdef0123456789abcdef" + _widevine_cenc_header(kid)

    cdm = CDM.new_default(init_data)
    request = cdm.get_license_request()

    # --- verify signature as a license server would -------------------------
    slr = {}
    for no, _wt, value in parse_fields(request):
        slr[no] = value
    assert slr[1] == 1, "SignedLicenseRequest type must be LICENSE_REQUEST"
    request_msg = slr[2]
    signature = slr[3]

    public_key = CDM.new_default(init_data).private_key.public_key()
    # cryptography hashes the raw message internally -- do NOT pre-hash here.
    public_key.verify(
        signature,
        request_msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA1()), salt_length=20),
        hashes.SHA1(),
    )

    # ClientIdentification blob must be embedded verbatim.
    client_ids = get_fields(request_msg, 1)
    expected_client = base64.b64decode(DEFAULT_CLIENT_ID_B64)
    assert client_ids and client_ids[0] == expected_client

    # --- craft a license response like Apple's key server ------------------
    session_key = bytes(range(16))
    content_key = bytes(reversed(range(16)))

    inner_msg = request_msg  # derivation input uses the marshaled Msg
    cmac_input = b"\x01ENCRYPTION\x00" + inner_msg + b"\x00\x00\x00\x80"
    cmac_key = aes_cmac(session_key, cmac_input)

    iv = b"\xab" * 16
    enc = Cipher(algorithms.AES(cmac_key), modes.CBC(iv)).encryptor()
    padded = content_key + b"\x10" * 16  # PKCS7 full block padding
    encrypted_key = enc.update(padded) + enc.finalize()

    key_container = encode_fields(
        [
            data_field(1, kid),
            data_field(2, iv),
            data_field(3, encrypted_key),
            vint_field(4, LICENSE_KEY_CONTAINER_CONTENT),
        ]
    )
    license_msg = encode_fields([raw_field(3, key_container)])
    wrapped_session_key = public_key.encrypt(
        session_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA1()),
            algorithm=hashes.SHA1(),
            label=None,
        ),
    )
    signed_license = encode_fields(
        [
            raw_field(2, license_msg),
            data_field(4, wrapped_session_key),
        ]
    )

    keys = cdm.get_license_keys(request, signed_license)
    assert len(keys) == 1
    assert keys[0].type == LICENSE_KEY_CONTAINER_CONTENT
    assert keys[0].value == content_key


def test_aes_cmac_rfc4493_vector():
    # RFC 4493 example 2: AES-CMAC of a 40-byte message with a known key.
    key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    message = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411"
    )
    expected = bytes.fromhex("dfa66747de9ae63030ca32611497c827")
    assert aes_cmac(key, message) == expected
