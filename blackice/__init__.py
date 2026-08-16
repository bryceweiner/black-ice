__version__ = "0.1.0"

# Must run before torch.hub / huggingface downloads; see blackice/_certs.py.
from . import _certs as _certs_module

_CA_BUNDLE = _certs_module.install()
