"""MAC address vendor lookup.

macOS ships no OUI database, and downloading the IEEE registry at start would
make a network fetch a precondition for naming a device. So: a small table of
the prefixes that actually turn up on a home network, and honesty about the
rest. A vendor string is a hint for the reader, never an identity -- it comes
off the wire and is trivially spoofed, so it travels as sensor text.
"""

from __future__ import annotations

# Prefix -> vendor. Lower-case, colon-separated, first three octets.
VENDORS: dict[str, str] = {
    # Apple
    "00:03:93": "Apple", "00:0a:27": "Apple", "00:1b:63": "Apple",
    "00:23:df": "Apple", "00:25:00": "Apple", "00:26:bb": "Apple",
    "04:0c:ce": "Apple", "3c:15:c2": "Apple", "40:a6:d9": "Apple",
    "44:d8:84": "Apple", "58:55:ca": "Apple", "6c:70:9f": "Apple",
    "7c:d1:c3": "Apple", "88:63:df": "Apple", "a4:5e:60": "Apple",
    "ac:bc:32": "Apple", "b8:e8:56": "Apple", "d0:e1:40": "Apple",
    "f0:18:98": "Apple", "f4:0f:24": "Apple",
    # Networking
    "00:0c:29": "VMware", "00:50:56": "VMware", "08:00:27": "VirtualBox",
    "00:1a:11": "Google", "3c:5a:b4": "Google", "f4:f5:d8": "Google",
    "00:17:88": "Philips Hue", "ec:b5:fa": "Philips Hue",
    "00:18:0a": "Meraki", "00:1d:7e": "Cisco-Linksys", "00:22:6b": "Cisco-Linksys",
    "00:14:bf": "Cisco-Linksys", "68:7f:74": "Cisco-Linksys",
    "24:a4:3c": "Ubiquiti", "78:8a:20": "Ubiquiti", "b4:fb:e4": "Ubiquiti",
    "fc:ec:da": "Ubiquiti", "74:ac:b9": "Ubiquiti",
    "00:09:5b": "Netgear", "20:4e:7f": "Netgear", "a0:40:a0": "Netgear",
    "00:1f:33": "Netgear", "c0:3f:0e": "Netgear",
    "00:05:5d": "D-Link", "1c:af:f7": "D-Link", "00:1c:f0": "D-Link",
    "00:14:6c": "TP-Link", "50:c7:bf": "TP-Link", "a4:2b:b0": "TP-Link",
    "ec:08:6b": "TP-Link", "60:32:b1": "TP-Link",
    "00:0d:b9": "PC Engines", "00:15:5d": "Microsoft Hyper-V",
    # Phones, tablets, consoles
    "00:1a:8a": "Samsung", "78:47:1d": "Samsung", "8c:77:12": "Samsung",
    "b4:79:a7": "Samsung", "e8:50:8b": "Samsung",
    "00:0f:de": "Sony", "00:19:c5": "Sony", "78:c8:81": "Sony",
    "00:17:ab": "Nintendo", "e8:4e:ce": "Nintendo",
    "00:1d:d8": "Microsoft", "7c:1e:52": "Microsoft", "60:45:bd": "Microsoft",
    "00:24:e4": "Withings", "18:b4:30": "Nest", "64:16:66": "Nest",
    "44:65:0d": "Amazon", "68:37:e9": "Amazon", "fc:65:de": "Amazon",
    "ac:63:be": "Amazon", "50:dc:e7": "Amazon",
    # Cameras and IoT, the usual suspects on a monitored network
    "00:12:12": "Hikvision", "c0:56:e3": "Hikvision", "44:19:b6": "Hikvision",
    "00:02:d1": "Vivotek", "00:80:f0": "Panasonic", "3c:ef:8c": "Dahua",
    "e0:50:8b": "Dahua", "9c:8e:cd": "Dahua",
    "b0:c5:54": "D-Link IoT", "ac:cf:23": "Espressif", "24:0a:c4": "Espressif",
    "30:ae:a4": "Espressif", "8c:aa:b5": "Espressif", "cc:50:e3": "Espressif",
    "b8:27:eb": "Raspberry Pi", "dc:a6:32": "Raspberry Pi",
    "e4:5f:01": "Raspberry Pi", "d8:3a:dd": "Raspberry Pi",
    "00:1e:06": "Wibrain", "5c:cf:7f": "Espressif", "18:fe:34": "Espressif",
    # Printers
    "00:21:5a": "HP", "3c:d9:2b": "HP", "9c:b6:54": "HP",
    "00:00:48": "Epson", "00:26:ab": "Epson", "00:1e:8f": "Canon",
    "00:80:77": "Brother", "00:1b:a9": "Brother",
}

# Locally administered addresses have the second-least-significant bit of the
# first octet set. Modern phones use them for MAC randomisation, so seeing one
# is normal -- but it also means the address is not a stable identity.
LOCALLY_ADMINISTERED = "randomised (locally administered)"


def normalise(mac: str) -> str:
    """Canonical lower-case colon form, or "" if it is not a MAC at all."""
    raw = (mac or "").strip().lower().replace("-", ":").replace(".", ":")
    parts = [p for p in raw.split(":") if p]
    if len(parts) != 6:
        return ""
    try:
        octets = [int(p, 16) for p in parts]
    except ValueError:
        return ""
    if any(o > 0xFF or o < 0 for o in octets):
        return ""
    return ":".join(f"{o:02x}" for o in octets)


def is_randomised(mac: str) -> bool:
    mac = normalise(mac)
    if not mac:
        return False
    return bool(int(mac.split(":")[0], 16) & 0b10)


def vendor(mac: str) -> str:
    """Best-effort vendor name. "" when we genuinely do not know."""
    mac = normalise(mac)
    if not mac:
        return ""
    if is_randomised(mac):
        return LOCALLY_ADMINISTERED
    return VENDORS.get(mac[:8], "")
