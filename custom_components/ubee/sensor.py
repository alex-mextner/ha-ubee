"""Ubee router connected-devices sensor.

Exposes the router's current DHCP/connected-device list (MAC + IP) as a single
sensor whose ``devices`` attribute is a ready-to-render list. Because Ubee
firmware often reports no hostnames (the DHCP table is MAC/IP only), the device
identity is resolved heuristically so most devices get a meaningful label
without any manual naming:

1. Home Assistant device registry — match the router MAC against a known
   device's network-MAC connection and reuse its (user) name. This auto-labels
   anything HA already manages (ESPHome nodes, TVs, speakers, media players,
   smart plugs, ...).
2. Router-provided hostname, if the model exposes one.
3. OUI vendor lookup for a few common makers.
4. Locally-administered (randomised) MAC detection — typically phones.
5. Fallback to the IP address.
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

import voluptuous as vol
from pyubee import Ubee

from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=120)

CONF_MODEL = "model"
DEFAULT_MODEL = "detect"

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_MODEL, default=DEFAULT_MODEL): cv.string,
    }
)

_IP_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

# Small, high-confidence OUI -> vendor map. The device-registry match above
# covers anything HA already knows; this only helps unmanaged gear.
OUI_VENDORS: dict[str, str] = {
    # Espressif (ESP8266/ESP32)
    "240AC4": "Espressif", "246F28": "Espressif", "30AEA4": "Espressif",
    "3C71BF": "Espressif", "483FDA": "Espressif", "4C11AE": "Espressif",
    "7C9EBD": "Espressif", "84CCA8": "Espressif", "8CAAB5": "Espressif",
    "A020A6": "Espressif", "A47B9D": "Espressif", "B4E62D": "Espressif",
    "BCDDC2": "Espressif", "C44F33": "Espressif", "CC50E3": "Espressif",
    "D8A01D": "Espressif", "DC4F22": "Espressif", "E072A1": "Espressif",
    "ECFABC": "Espressif", "F4CFA2": "Espressif",
    # Raspberry Pi
    "B827EB": "Raspberry Pi", "DCA632": "Raspberry Pi", "E45F01": "Raspberry Pi",
    "28CDC1": "Raspberry Pi", "D83ADD": "Raspberry Pi",
    # Sonos / Google / Amazon / Sonoff
    "5CAAFD": "Sonos", "B8E937": "Sonos",
    "F4F5D8": "Google", "1CF29A": "Google", "54600B": "Google",
    "FCA667": "Amazon", "44650D": "Amazon", "68DBF5": "Amazon",
}

_ICON_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("camera", "cam", "cctv", "doorbell"), "mdi:cctv"),
    (("tv", "televi", "samsung", "lg ", "bravia"), "mdi:television"),
    (("projector", "beamer"), "mdi:projector"),
    (("kodi", "htpc", "mini-pc", "minipc", "nuc"), "mdi:kodi"),
    (("chromecast", "cast", "shield", "androidtv", "android tv"), "mdi:cast"),
    (("station", "yandex", "alice", "speaker", "sonos", "echo"), "mdi:speaker"),
    (("plug", "socket", "outlet", "shelly"), "mdi:power-socket-de"),
    (("sensor", "aqara", "motion"), "mdi:motion-sensor"),
    (("light", "bulb", "lamp", "led"), "mdi:lightbulb"),
    (("phone", "iphone", "pixel", "galaxy", "mobile"), "mdi:cellphone"),
    (("macbook", "laptop", "imac", "pc", "desktop"), "mdi:laptop"),
    (("printer",), "mdi:printer"),
    (("router", "gateway", "ubee", "modem"), "mdi:router-network"),
    (("esp", "esphome"), "mdi:chip"),
]


def _is_random_mac(mac: str) -> bool:
    """True if the MAC has the locally-administered bit set (randomised)."""
    try:
        return bool(int(mac[1], 16) & 0x2)
    except (ValueError, IndexError):
        return False


def _ip_sort_key(ip: str | None) -> tuple[int, ...]:
    if ip and _IP_RE.match(ip):
        return tuple(int(o) for o in ip.split("."))
    return (999, 999, 999, 999)


def _guess_icon(name: str | None, vendor: str | None) -> str:
    haystack = f"{name or ''} {vendor or ''}".lower()
    for keywords, icon in _ICON_KEYWORDS:
        if any(k in haystack for k in keywords):
            return icon
    return "mdi:lan-connect"


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Ubee connected-devices sensor."""
    ubee = Ubee(
        config[CONF_HOST],
        config[CONF_USERNAME],
        config[CONF_PASSWORD],
        config[CONF_MODEL],
    )
    async_add_entities(
        [UbeeDevicesSensor(hass, ubee, config[CONF_HOST])], update_before_add=True
    )


class UbeeDevicesSensor(SensorEntity):
    """Sensor exposing the router's connected devices with heuristic names."""

    _attr_icon = "mdi:router-network"
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, hass: HomeAssistant, ubee: Ubee, host: str) -> None:
        self.hass = hass
        self._ubee = ubee
        self._attr_name = "Ubee Devices"
        self._attr_unique_id = f"ubee_devices_{host}"
        self._devices: list[dict[str, Any]] = []

    @property
    def native_value(self) -> int:
        return len(self._devices)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"devices": self._devices}

    def _fetch(self) -> dict[str, str]:
        """Blocking router query (runs in the executor)."""
        if not self._ubee.session_active():
            self._ubee.login()
        return self._ubee.get_connected_devices() or {}

    def _registry_names(self) -> dict[str, str]:
        """Map normalised MAC -> friendly name from the HA device registry."""
        reg = dr.async_get(self.hass)
        names: dict[str, str] = {}
        for device in reg.devices.values():
            name = device.name_by_user or device.name
            if not name:
                continue
            for conn_type, conn_val in device.connections:
                if conn_type == dr.CONNECTION_NETWORK_MAC:
                    names[dr.format_mac(conn_val)] = name
        return names

    async def async_update(self) -> None:
        try:
            raw = await self.hass.async_add_executor_job(self._fetch)
        except Exception as err:  # noqa: BLE001 - router/network can fail many ways
            _LOGGER.warning("Ubee device query failed: %s", err)
            return

        mac2name = self._registry_names()
        devices: list[dict[str, Any]] = []
        for mac, value in raw.items():
            norm_mac = dr.format_mac(mac)
            if _IP_RE.match(str(value)):
                ip: str | None = value
                hostname: str | None = None
            else:
                ip, hostname = None, (value or None)

            vendor = OUI_VENDORS.get(norm_mac.replace(":", "").upper()[:6])
            registry_name = mac2name.get(norm_mac)
            random_mac = _is_random_mac(norm_mac)

            if registry_name:
                name, identified = registry_name, True
            elif hostname:
                name, identified = hostname, True
            elif vendor:
                name, identified = f"{vendor} device", False
            elif random_mac:
                name, identified = "Private device", False
            else:
                name, identified = ip or norm_mac, False

            devices.append(
                {
                    "name": name,
                    "ip": ip,
                    "mac": norm_mac,
                    "vendor": vendor,
                    "identified": identified,
                    "random_mac": random_mac,
                    "icon": _guess_icon(name if identified else vendor, vendor),
                }
            )

        devices.sort(key=lambda d: (not d["identified"], _ip_sort_key(d["ip"])))
        self._devices = devices
