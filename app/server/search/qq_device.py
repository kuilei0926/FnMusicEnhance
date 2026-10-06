#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Best-effort QQ Music device identity with a local cache.

Device payload and QIMEI protocol adapted from CharlesPikachu/musicdl.
Copyright (c) 2018-2026 CharlesPikachu. PolyForm Noncommercial 1.0.0.
See third_party/musicdl-LICENSE.txt for the applicable notice and terms.

The Tencent QIMEI endpoint is optional. If it is unavailable, the historical
fallback remains usable so a transient device service outage cannot stop search.
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json
import os
import random
import string
import threading
import time
from uuid import uuid4

from . import net

FALLBACK_QIMEI36 = "6c9d3cd110abca9b16311cee10001e717614"
QIMEI_ENDPOINT = "https://api.tencentmusic.com/tme/trpc/proxy"
APP_KEY = "0AND0HD6FE4HY80F"
SECRET = "ZdJqM15EeO2zWc08"
PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDEIxgwoutfwoJxcGQeedgP7FG9qaIuS0qzfR8gWkrkTZKM2iWHn2ajQpBRZjMSoSf6+KJGvar2ORhBfpD
XyVtZCKpqLQ+FLkpncClKVIrBwv6PHyUvuCb0rIarmgDnzkfQAqVufEtR64iazGDKatvJ9y6B9NMbHddGSAUmRTCrHQIDAQAB
-----END PUBLIC KEY-----"""

_LOCK = threading.RLock()
_CACHED = None


def _cache_path():
    app_log = os.environ.get("LOG_FILE", "/var/apps/FnMusicEnhance/var/app.log")
    return os.environ.get("QQ_QIMEI_CACHE") or os.path.join(
        os.path.dirname(app_log), "qq_device.json")


def _valid(value):
    return isinstance(value, str) and len(value) == 36 and value.isalnum()


def _mask(value):
    if not value:
        return "-"
    return str(value)[:8] + "..." + str(value)[-4:]


def _load_cache():
    try:
        with open(_cache_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) and _valid(data.get("q36")) else None
    except Exception:
        return None


def _save_cache(data):
    path = _cache_path()
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp-%d" % os.getpid()
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


@dataclass
class _Device:
    display: str = field(default_factory=lambda: "QMAPI.%d.001" % random.randint(100000, 999999))
    product: str = "iarim"
    device: str = "sagit"
    board: str = "eomam"
    model: str = "MI 6"
    fingerprint: str = field(default_factory=lambda: "xiaomi/iarim/sagit:10/eomam.200122.001/%d:user/release-keys" % random.randint(1000000, 9999999))
    boot_id: str = field(default_factory=lambda: str(uuid4()))
    proc_version: str = field(default_factory=lambda: "Linux 5.4.0-54-generic-%s (android-build@google.com)" % "".join(random.choices(string.ascii_letters + string.digits, k=8)))
    imei: str = field(default_factory=lambda: "".join(str(random.randint(0, 9)) for _ in range(15)))
    brand: str = "Xiaomi"
    version_release: str = "10"
    version_sdk: int = 29
    android_id: str = field(default_factory=lambda: os.urandom(8).hex())


def _random_beacon_id():
    month = datetime.now().strftime("%Y-%m-") + "01"
    rand1, rand2 = random.randint(100000, 999999), random.randint(100000000, 999999999)
    parts = []
    for index in range(1, 41):
        if index in (1, 2, 13, 14, 17, 18, 21, 22, 25, 26, 29, 30, 33, 34, 37, 38):
            value = "%s%d.%d" % (month, rand1, rand2)
        elif index == 3:
            value = "0000000000000000"
        elif index == 4:
            value = "".join(random.choices("123456789abcdef", k=16))
        else:
            value = str(random.randint(0, 9999))
        parts.append("k%d:%s" % (index, value))
    return ";".join(parts) + ";"


def _device_payload(device):
    fixed_rand = random.randint(0, 14400)
    reserved = {
        "harmony": "0", "oz": "UhYmelwouA+V2nPWbOvLTgN2/m8jwGB+yUB5v9tysQg=",
        "oo": "Xecjt+9S1+f8Pz2VLSxgpw==", "kelong": "0",
        "uptimes": (datetime.now() - timedelta(seconds=fixed_rand)).strftime("%Y-%m-%d %H:%M:%S"),
        "clone": "0", "containe": "", "multiUser": "0", "bod": device.brand,
        "dv": device.device, "firstLevel": "", "manufact": device.brand,
        "name": device.model, "host": "se.infra", "kernel": device.proc_version,
    }
    return {
        "androidId": device.android_id, "platformId": 1, "appKey": APP_KEY,
        "appVersion": "13.2.5.8", "beaconIdSrc": _random_beacon_id(),
        "brand": device.brand, "channelId": "10003505", "cid": "", "imei": device.imei,
        "imsi": "", "mac": "", "model": device.model, "networkType": "unknown",
        "oaid": "", "osVersion": "Android %s,level %d" % (device.version_release, device.version_sdk),
        "qimei": "", "qimei36": "", "sdkVersion": "1.2.13.6",
        "targetSdkVersion": "33", "audit": "", "userId": "{}",
        "packageId": "com.tencent.qqmusic", "deviceType": "Phone", "sdkName": "",
        "reserved": json.dumps(reserved, ensure_ascii=False, separators=(",", ":")),
    }


def _rsa_encrypt(content):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_public_key(PUBLIC_KEY.encode("ascii"))
    return key.encrypt(content, padding.PKCS1v15())


def _aes_encrypt(key, content):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    padding_size = 16 - len(content) % 16
    content += bytes([padding_size]) * padding_size
    cipher = Cipher(algorithms.AES(key), modes.CBC(key))
    encryptor = cipher.encryptor()
    return encryptor.update(content) + encryptor.finalize()


def _md5(*values):
    digest = hashlib.md5()
    for value in values:
        digest.update(value if isinstance(value, bytes) else str(value).encode("utf-8"))
    return digest.hexdigest()


def _obtain_remote():
    device = _Device()
    timestamp = int(time.time())
    crypt_key = "".join(random.choices("adbcdef1234567890", k=16)).encode("ascii")
    nonce = "".join(random.choices("adbcdef1234567890", k=16))
    key = base64.b64encode(_rsa_encrypt(crypt_key)).decode("ascii")
    params = base64.b64encode(_aes_encrypt(
        crypt_key, json.dumps(_device_payload(device), ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))).decode("ascii")
    extra = '{"appKey":"%s"}' % APP_KEY
    sign = _md5(key, params, str(timestamp * 1000), nonce, SECRET, extra)
    payload = {
        "app": 0, "os": 1,
        "qimeiParams": {
            "key": key, "params": params, "time": str(timestamp),
            "nonce": nonce, "sign": sign, "extra": extra,
        },
    }
    headers = {
        "Host": "api.tencentmusic.com", "method": "GetQimei",
        "service": "trpc.tme_datasvr.qimeiproxy.QimeiProxy",
        "appid": "qimei_qq_android",
        "sign": _md5("qimei_qq_androidpzAuCmaFAaFaHrdakPjLIEqKrGnSOOvH", str(timestamp)),
        "user-agent": "QQMusic", "timestamp": str(timestamp),
    }
    data, meta = net.post_json_parsed_detailed(
        QIMEI_ENDPOINT, json_body=payload, headers=headers,
        timeout=float(os.environ.get("QQ_QIMEI_TIMEOUT", "5")),
        context={"operation": "GetQimei"})
    if not meta.get("ok"):
        raise RuntimeError(meta.get("errorType") or "qimei request failed")
    raw = data.get("data") if isinstance(data, dict) else None
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict):
        raw = raw["data"]
    if not isinstance(raw, dict) or not _valid(raw.get("q36")):
        raise ValueError("invalid qimei response")
    return {"q16": str(raw.get("q16") or ""), "q36": str(raw["q36"]),
            "createdAt": datetime.now().isoformat(timespec="seconds"),
            "source": "tencent"}


def get_qimei36():
    global _CACHED
    if str(os.environ.get("QQ_DYNAMIC_QIMEI", "1")).lower() in ("0", "false", "off"):
        return FALLBACK_QIMEI36
    with _LOCK:
        if _CACHED and _valid(_CACHED.get("q36")):
            return _CACHED["q36"]
        cached = _load_cache()
        if cached:
            _CACHED = cached
            net.log_qq_event("QIMEI", source="cache", qimei36=_mask(cached["q36"]))
            return cached["q36"]
        try:
            _CACHED = _obtain_remote()
        except Exception as error:
            _CACHED = {"q36": FALLBACK_QIMEI36, "source": "fallback"}
            net.log_qq_event("QIMEI", level=__import__("logging").WARNING,
                             source="fallback", reason=type(error).__name__,
                             qimei36=_mask(FALLBACK_QIMEI36))
            return FALLBACK_QIMEI36
        try:
            _save_cache(_CACHED)
        except Exception as error:
            net.log_qq_event("QIMEI_CACHE_ERROR", level=__import__("logging").WARNING,
                             reason=type(error).__name__)
        net.log_qq_event("QIMEI", source="tencent", qimei36=_mask(_CACHED["q36"]))
        return _CACHED["q36"]
