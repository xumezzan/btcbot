"""SSL helpers that use certifi's CA bundle for local Python installs."""
import ssl

import certifi


def create_ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())
