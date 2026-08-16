"""Point stdlib SSL at a real CA bundle.

The python.org framework build ships without one, so `ssl` has no default
cafile. Anything using urllib -- torch.hub fetching Silero VAD, Hugging Face
downloaders -- then fails with "unable to get local issuer certificate", which
torch.hub reports as "there is no internet connection". Set once, at import.
"""

from __future__ import annotations

import os
import ssl


def install() -> str | None:
    if ssl.get_default_verify_paths().cafile:
        return None  # the interpreter already has a bundle
    try:
        import certifi
    except ImportError:
        return None
    bundle = certifi.where()
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        os.environ.setdefault(var, bundle)
    return bundle
