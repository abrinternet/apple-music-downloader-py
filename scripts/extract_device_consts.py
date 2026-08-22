"""One-shot generator for amdl/runv3/device_consts.py.

Reads the Go implementation's utils/runv3/cdm/consts.go and emits the default
device material as a Python module so both implementations share one identity.
"""

import base64
import re
import sys
from pathlib import Path

GO_CONSTS = Path(sys.argv[1])
OUT = Path("amdl/runv3/device_consts.py")

src = GO_CONSTS.read_text(encoding="utf-8")
key_m = re.search(r'DefaultPrivateKey = "(.*?)"', src, re.S)
cid_m = re.search(r'DefaultClientdIDBase64 := "(.*?)"', src, re.S)
assert key_m and cid_m, "could not locate device constants in consts.go"

pem = key_m.group(1).replace("\\n", "\n")
cid_b64 = cid_m.group(1)
assert pem.startswith("-----BEGIN RSA PRIVATE KEY-----")
base64.b64decode(cid_b64)

body = '"""Default Widevine device material.\n\nExtracted mechanically from the Go implementation\'s\nutils/runv3/cdm/consts.go so both implementations share one identity.\n"""\n\n'
body += "DEFAULT_PRIVATE_KEY_PEM = " + repr(pem) + "\n\n"
body += "DEFAULT_CLIENT_ID_B64 = " + repr(cid_b64) + "\n"
OUT.write_text(body, encoding="utf-8")

from cryptography.hazmat.primitives.serialization import load_pem_private_key

key = load_pem_private_key(pem.encode(), None)
print(f"written {OUT}: RSA {key.key_size} bits, client blob {len(base64.b64decode(cid_b64))} bytes")
