"""Software identity announced over XMPP (resource, disco, XEP-0092)."""
from __future__ import annotations

import platform
from importlib.metadata import PackageNotFoundError, version as _pkg_version

SOFTWARE_NAME = "j2i"
CAPS_NODE = "https://fsky.io/projects/j2i"


def package_version() -> str:
    try:
        return _pkg_version("j2i")
    except PackageNotFoundError:
        return ""


def software_label(*, hide_version: bool = False) -> str:
    """Human-readable name for resource and disco identity: 'j2i' or 'j2i 1.2.0'."""
    ver = package_version()
    if hide_version or not ver:
        return SOFTWARE_NAME
    return f"{SOFTWARE_NAME} {ver}"


def software_version(*, hide_version: bool = False) -> str:
    """XEP-0092 <version/> text. Empty omits the element."""
    if hide_version:
        return ""
    return package_version()


def software_os(*, hide_os: bool = False) -> str:
    """XEP-0092 <os/> text (e.g. Linux). Empty omits the element."""
    if hide_os:
        return ""
    return platform.system()


def xep_0092_config(
    *, hide_version: bool = False, hide_os: bool = False
) -> dict[str, str]:
    return {
        "software_name": SOFTWARE_NAME,
        "version": software_version(hide_version=hide_version),
        "os": software_os(hide_os=hide_os),
    }
