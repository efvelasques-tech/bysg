"""HTTPS trust configuration without disabling certificate or hostname checks."""
from __future__ import annotations

import ssl
import sys
from pathlib import Path


def verified_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    # Some python.org macOS installations have no OpenSSL CA bundle. Use the
    # existing system bundle only when Python has loaded no trusted CA roots.
    # Keep configured trust stores and TLS verification settings unchanged.
    system_bundle = Path("/etc/ssl/cert.pem")
    if sys.platform == "darwin" and not context.get_ca_certs() and system_bundle.is_file():
        context.load_verify_locations(cafile=str(system_bundle))
    return context
