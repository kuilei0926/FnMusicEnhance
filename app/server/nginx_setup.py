#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import io
import os
import secrets
import struct
import subprocess
import sys
import time
import zipfile
import zlib

# ---------------------------------------------------------------------------
# 常量 / 可配置路径
# ---------------------------------------------------------------------------

CONF_NAME = "fnmusic_enhance.conf"
DEFAULT_SOCK = "/var/run/fnmusic_enhance.sock"

# recover 模块把 ng.conf.zip 解压到 /usr/trim/nginx/conf/, nginx 只 include
# conf.d/*.conf, 因此 zip 里的条目路径必须带 conf.d/ 前缀
ZIP_ENTRY = "conf.d/" + CONF_NAME

NGINX_BIN = os.environ.get("TRIM_NGINX_BIN", "/usr/trim/nginx/sbin/nginx")
CONF_DIR = os.environ.get("TRIM_CONF_DIR", "/usr/trim/nginx/conf/conf.d")
RESTORE_ZIP = os.environ.get("TRIM_RESTORE_ZIP", "/usr/trim/share/.restore/ng.conf.zip")

# recover 模块出错日志格式串, 用于精确定位 key 缓冲区
PWD_STR = b"archive_read_add_passphrase: %s, key: %s"


def log(msg):
    line = "[%s] [nginx_setup] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    lf = os.environ.get("LOG_FILE", "")
    if lf:
        try:
            with open(lf, "a", encoding="utf-8", errors="replace") as f:
                f.write(line)
        except Exception:
            pass
    else:
        print(line, end="")


# ---------------------------------------------------------------------------
# 1) 从 nginx 二进制提取 ng.conf.zip 密码
# ---------------------------------------------------------------------------

_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    _CRC_TABLE.append(_c)


def _crc_update(crc, b):
    return (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]


def _xor_pass(blob):
    return bytes(b ^ 0xAA for b in blob)


def _is_plausible(pw):
    """密码应为 24 字节, XOR 0xAA 后全为字母数字, 且非整段重复(填充区)。"""
    if len(pw) != 24:
        return False
    if not all(
        (c >= 0x41 and c <= 0x5A) or (c >= 0x61 and c <= 0x7A) or (c >= 0x30 and c <= 0x39)
        for c in pw
    ):
        return False
    if len(set(pw)) == 1:
        return False
    return True


def _elf_sections(data):
    """解析 ELF section headers -> {name: (file_offset, vaddr, size)}"""
    out = {}
    if len(data) < 0x40 or data[:4] != b"\x7fELF":
        return out
    e_shoff = struct.unpack_from("<Q", data, 0x28)[0]
    e_shentsize = struct.unpack_from("<H", data, 0x3A)[0]
    e_shnum = struct.unpack_from("<H", data, 0x3C)[0]
    e_shstrndx = struct.unpack_from("<H", data, 0x3E)[0]
    if e_shentsize < 64 or e_shoff + e_shnum * e_shentsize > len(data):
        return out
    str_off = struct.unpack_from("<Q", data, e_shoff + e_shstrndx * e_shentsize + 0x18)[0]
    for i in range(e_shnum):
        s = e_shoff + i * e_shentsize
        name_off = struct.unpack_from("<I", data, s)[0]
        end = data.find(b"\0", str_off + name_off)
        if end < 0:
            continue
        name = data[str_off + name_off:end].decode("utf-8", "replace")
        vaddr, off, size = struct.unpack_from("<QQQ", data, s + 0x10)
        out[name] = (off, vaddr, size)
    return out


def _file_to_vaddr(secs, off):
    for _, (soff, svaddr, ssize) in secs.items():
        if soff <= off < soff + ssize:
            return svaddr + (off - soff)
    return off


def _vaddr_to_off(secs, vaddr):
    for _, (soff, svaddr, ssize) in secs.items():
        if svaddr <= vaddr < svaddr + ssize:
            return soff + (vaddr - svaddr)
    return None


EM_X86_64 = 62
EM_AARCH64 = 183


def _elf_machine(data):
    """读 ELF e_machine: 62=x86-64, 183=AArch64。"""
    if len(data) < 0x14 or data[:4] != b"\x7fELF":
        return None
    return struct.unpack_from("<H", data, 0x12)[0]


def _lea_rip_targets(text, text_vaddr):
    """扫描 .text, 找 64 位 RIP 相对 lea (REX 8D ModRM mod=00 rm=101), 返回 (指令地址, 目标地址)"""
    out = []
    n = len(text)
    i = 0
    while i + 7 <= n:
        j = i
        if 0x40 <= text[j] <= 0x4F:
            j += 1
        if j + 6 > n:
            break
        if text[j] != 0x8D:
            i += 1
            continue
        modrm = text[j + 1]
        if (modrm >> 6) != 0 or (modrm & 7) != 5:
            i += 1
            continue
        disp = struct.unpack_from("<i", text, j + 2)[0]
        ins_addr = text_vaddr + i
        out.append((ins_addr, ins_addr + (j - i + 6) + disp))
        i += 1
    return out


_A64_ADR_MASK = 0x9F000000
_A64_ADR_VAL = 0x10000000
_A64_ADRP_VAL = 0x90000000
_A64_ADD_IMM_MASK = 0xFF000000
_A64_ADD_IMM_VAL = 0x91000000
_A64_LDR_LIT_MASK = 0xFF000000
_A64_LDR_LIT_VAL = 0x58000000


def _a64_sx(v, bits):
    s = 1 << (bits - 1)
    return (v ^ s) - s


def _a64_ref_targets(text, text_vaddr, data, secs):
    """扫描 .text, 提取 AArch64 内存引用 (指令地址, 目标地址)。

    - ADR: PC 相对字节级寻址, 直接给出目标地址
    - ADRP + ADD(imm): 页寻址 + 页内偏移, 合并为精确地址
    - LDR literal: 字面量池加载, 解引用池中 8 字节小端值
    """
    out = []
    pages = {}
    n = len(text)
    for i in range(0, n - 3, 4):
        w = struct.unpack_from("<I", text, i)[0]
        addr = text_vaddr + i
        if (w & _A64_ADR_MASK) == _A64_ADRP_VAL:
            imm = _a64_sx(((w >> 5) & 0x7FFFF) << 2 | ((w >> 29) & 3), 21)
            pages[addr] = ((addr & ~0xFFF) + (imm << 12), w & 0x1F)
        elif (w & _A64_ADR_MASK) == _A64_ADR_VAL:
            imm = _a64_sx(((w >> 5) & 0x7FFFF) << 2 | ((w >> 29) & 3), 21)
            out.append((addr, addr + imm))
        elif (w & _A64_LDR_LIT_MASK) == _A64_LDR_LIT_VAL:
            imm = _a64_sx((w >> 5) & 0x7FFFF, 19)
            lit = addr + (imm << 2)
            target = lit
            off = _vaddr_to_off(secs, lit)
            if off is not None and off + 8 <= len(data):
                target = struct.unpack_from("<Q", data, off)[0]
            out.append((addr, target))
    for addr, (page, rd) in pages.items():
        off = addr - text_vaddr
        for j in range(off + 4, min(off + 24, n - 3), 4):
            w = struct.unpack_from("<I", text, j)[0]
            if (w & _A64_ADD_IMM_MASK) == _A64_ADD_IMM_VAL and ((w >> 5) & 0x1F) == rd:
                imm12 = (w >> 10) & 0xFFF
                shift = 12 if (w >> 22) & 1 else 0
                out.append((addr, page + (imm12 << shift)))
                break
    out.sort(key=lambda x: x[0])
    return out


def _extract_precise(data):
    """精确法: 定位 PWD_STR 的引用, 回溯到 key 缓冲区。"""
    secs = _elf_sections(data)
    t = secs.get(".text")
    if not t:
        return None
    t_off, t_vaddr, t_size = t
    pwd_at = data.find(PWD_STR)
    if pwd_at < 0:
        return None
    pwd_vaddr = _file_to_vaddr(secs, pwd_at)
    text = data[t_off:t_off + t_size]
    machine = _elf_machine(data)
    if machine == EM_X86_64:
        refs = _lea_rip_targets(text, t_vaddr)
    elif machine == EM_AARCH64:
        refs = _a64_ref_targets(text, t_vaddr, data, secs)
    else:
        return None
    site = None
    for ins, target in refs:
        if target == pwd_vaddr:
            site = ins
            break
    if site is None:
        return None
    for ins, target in reversed(refs):
        if ins >= site:
            continue
        if site - ins > 4096:
            break
        off = _vaddr_to_off(secs, target)
        if off is None:
            off = target  # 回退: 旧行为 vaddr 直接当文件偏移
        if off + 24 > len(data):
            continue
        pw = _xor_pass(data[off:off + 24])
        if _is_plausible(pw):
            return pw
    return None


def _extract_heuristic(data):
    """启发式回退: 扫全文件找 XOR 0xAA 后为 24 位字母数字的窗口。"""
    out = []
    for i in range(len(data) - 24):
        pw = _xor_pass(data[i:i + 24])
        if _is_plausible(pw):
            out.append(pw)
    return out


def get_password():
    if not os.path.exists(NGINX_BIN):
        log("nginx 二进制不存在: %s" % NGINX_BIN)
        return None
    with open(NGINX_BIN, "rb") as f:
        data = f.read()
    arch = {EM_X86_64: "x86-64", EM_AARCH64: "AArch64"}.get(
        _elf_machine(data), "unknown")
    log("nginx 二进制架构: %s" % arch)
    pw = _extract_precise(data)
    if pw is None:
        cands = _extract_heuristic(data)
        if cands:
            pw = cands[0]
    if pw is None:
        log("无法从 %s 提取 zip 密码" % NGINX_BIN)
    else:
        log("已提取 ng.conf.zip 密码")
    return pw


# ---------------------------------------------------------------------------
# 2) ZipCrypto (PKWARE traditional) 加密写 zip
#    注意: update_keys 使用「明文字节」(与 zipfile/_ZipDecrypter、原厂及
#    libarchive 互操作, 已用 bkcrack 恢复出与原厂一致的密钥验证)
# ---------------------------------------------------------------------------


class ZipCrypto:
    def __init__(self, password):
        self.k0, self.k1, self.k2 = 0x12345678, 0x23456789, 0x34567890
        for b in password:
            self.update(b)

    def update(self, b):
        self.k0 = _crc_update(self.k0, b) & 0xFFFFFFFF
        self.k1 = (self.k1 + (self.k0 & 0xFF)) & 0xFFFFFFFF
        self.k1 = (self.k1 * 134775813 + 1) & 0xFFFFFFFF
        self.k2 = _crc_update(self.k2, (self.k1 >> 24) & 0xFF) & 0xFFFFFFFF

    def keystream(self):
        t = self.k2 | 2
        return ((t * (t ^ 1)) >> 8) & 0xFF

    def encrypt(self, data, crc):
        out = bytearray()
        for _ in range(11):                      # 11 个随机头字节
            b = secrets.randbelow(256)
            c = b ^ self.keystream()
            out.append(c)
            self.update(b)                       # 用明文更新
        b = (crc >> 24) & 0xFF                   # 第12字节 check byte = CRC 高字节
        out.append(b ^ self.keystream())
        self.update(b)
        for p in data:
            c = p ^ self.keystream()
            out.append(c)
            self.update(p)
        return bytes(out)


def _dos_dt(dt):
    return ((dt[3] << 11) | (dt[4] << 5) | (dt[5] // 2)), \
           (((dt[0] - 1980) << 9) | (dt[1] << 5) | dt[2])


def build_zip(entries, password):
    """entries: [(name, data, date_time, mode), ...] -> ZipCrypto 加密 zip 字节"""
    buf = io.BytesIO()
    central = []
    offset = 0
    for name, data, dt, mode in entries:
        crc = zlib.crc32(data) & 0xFFFFFFFF
        enc = ZipCrypto(password).encrypt(data, crc)
        bname = name.encode("utf-8")
        dtime, ddate = _dos_dt(dt)
        buf.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 1, 0, dtime, ddate,
                              crc, len(enc), len(data), len(bname), 0))
        buf.write(bname)
        buf.write(enc)
        central.append(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 0x031E, 20, 1, 0,
                                   dtime, ddate, crc, len(enc), len(data), len(bname),
                                   0, 0, 0, 0, mode, offset) + bname)
        offset += 30 + len(bname) + len(enc)
    cd = b"".join(central)
    buf.write(cd)
    buf.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries),
                          len(cd), offset, 0))
    return buf.getvalue()


def _read_entry_payload(f, info, password):
    """按 local header 直接读出并解密单个条目。

    厂商 zip 由 libarchive 生成, 设置 data descriptor 标志时会用 DOS 时间
    高字节做校验字节(而非 CRC 高字节), 旧版 Python 的 zipfile 不认识该
    约定, 密码正确也会误报 Bad password。这里两种约定都接受。
    """
    f.seek(info.header_offset)
    hdr = f.read(30)
    if len(hdr) < 30 or hdr[:4] != b"PK\x03\x04":
        raise RuntimeError("%s: 无效的 local header" % info.filename)
    _, flags, method, dtime, _, _, csize, _, nl, el = \
        struct.unpack_from("<HHHHHIIIHH", hdr, 4)
    if csize == 0:
        csize = info.compress_size
    f.seek(info.header_offset + 30 + nl + el)
    if flags & 0x1:
        enc = f.read(csize)
        z = ZipCrypto(password)
        check_crc = (info.CRC >> 24) & 0xFF
        check_time = (dtime >> 8) & 0xFF
        plain = bytearray()
        for i, c in enumerate(enc):
            p = c ^ z.keystream()
            z.update(p)
            if i < 11:
                continue
            if i == 11:
                if p != check_crc and p != check_time:
                    raise RuntimeError("Bad password for file %r" % info.filename)
            else:
                plain.append(p)
        if info.CRC and zlib.crc32(plain) & 0xFFFFFFFF != info.CRC:
            raise RuntimeError("%s: CRC 校验失败" % info.filename)
    else:
        plain = f.read(csize)
    if method == 8:
        return zlib.decompress(plain, -15)
    if method != 0:
        raise RuntimeError("%s: 不支持的压缩方法 %d" % (info.filename, method))
    return bytes(plain)


def _read_entries(zip_path, password):
    """读回全部条目(含目录), 密码错误时抛异常。

    解密不依赖 zipfile(见 _read_entry_payload), 避免旧版 Python 对
    data descriptor 校验字节约定的兼容问题。
    """
    with zipfile.ZipFile(zip_path) as zf:
        with open(zip_path, "rb") as f:
            entries = []
            for info in zf.infolist():
                if info.is_dir():
                    mode = (info.external_attr >> 16) & 0xFFFF or 0o755
                    entries.append((info.filename, b"", info.date_time, mode))
                    continue
                payload = _read_entry_payload(f, info, password)
                mode = (info.external_attr >> 16) & 0xFFFF or 0o644
                entries.append((info.filename, payload, info.date_time, mode))
    return entries


def _password_valid(zip_path, password):
    """验证密码能否完整读回 zip。"""
    try:
        _read_entries(zip_path, password)
        return True
    except Exception as e:
        log("zip 密码验证失败: %s" % e)
        return False


def _direct_inject(conf_path, content):
    """zip 方案不可行时: 直接写 conf 文件并重启 nginx。"""
    tmp = conf_path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, conf_path)
    except Exception as e:
        log("直接写入 %s 失败: %s" % (conf_path, e))
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    log("已直接写入 %s, 重启 nginx 生效" % conf_path)
    return restart_nginx()


def add_entry_to_zip(zip_path, password, name, data):
    """读全部条目 -> 加入新条目 -> ZipCrypto 重写 -> 原子替换(先备份)。"""
    try:
        entries = _read_entries(zip_path, password)
    except Exception as e:
        log("读取 %s 失败: %s" % (zip_path, e))
        return False

    now = time.localtime()
    entries.append((name, data,
                    (now.tm_year, now.tm_mon, now.tm_mday,
                     now.tm_hour, now.tm_min, now.tm_sec), 0o644))

    tmp = zip_path + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(build_zip(entries, password))
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        log("写临时文件失败: %s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    # 写回前自检: 能完整读回才算成功
    try:
        with zipfile.ZipFile(tmp) as v:
            v.setpassword(password)
            for n in v.namelist():
                if not n.endswith("/"):
                    v.read(n)
    except Exception as e:
        log("重写后的 zip 自检失败, 已放弃: %s" % e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    try:
        import shutil
        shutil.copy2(zip_path, zip_path + ".bak")   # 保留一份原始备份
        os.replace(tmp, zip_path)
    except Exception as e:
        log("替换 %s 失败: %s" % (zip_path, e))
        return False
    log("已更新 %s (%d 个条目)" % (zip_path, len(entries)))
    return True


# ---------------------------------------------------------------------------
# 3) conf 内容 / 重启 / 主流程
# ---------------------------------------------------------------------------


def build_conf_content():
    sock = os.environ.get("SOCK_PATH", DEFAULT_SOCK)
    return (
        "# FnMusicEnhance\n"
        "location /music-enhance/ {\n"
        "    proxy_pass http://unix:%s:/;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header X-Real-IP $remote_addr;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "}\n"
    ) % sock


def restart_nginx():
    try:
        r = subprocess.run(["systemctl", "restart", "trim_nginx"],
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        log("重启 trim_nginx 异常: %s" % e)
        return False
    if r.returncode != 0:
        log("systemctl restart trim_nginx 失败: %s" % r.stderr.strip())
    else:
        log("已重启 trim_nginx")
    return r.returncode == 0


def ensure_nginx_conf():
    """启动时调用: 保证 conf.d 里有我们的配置。"""
    conf_path = os.path.join(CONF_DIR, CONF_NAME)
    if os.path.exists(conf_path):
        log("%s 已存在, 无需处理" % conf_path)
        return 0

    log("%s 不存在, 开始注入 ng.conf.zip" % conf_path)
    password = get_password()
    if not password:
        return 1

    if not os.path.exists(RESTORE_ZIP):
        log("ng.conf.zip 不存在: %s" % RESTORE_ZIP)
        return 1

    if not _password_valid(RESTORE_ZIP, password):
        log("zip 密码不可用, 回退为直接写 conf 文件")
        content = build_conf_content().encode("utf-8")
        return 0 if _direct_inject(conf_path, content) else 1

    try:
        names = zipfile.ZipFile(RESTORE_ZIP).namelist()
    except Exception as e:
        log("读取 ng.conf.zip 失败: %s" % e)
        return 1

    if ZIP_ENTRY in names:
        log("%s 已在 zip 中, 重启 nginx 释放" % ZIP_ENTRY)
        return 0 if restart_nginx() else 1

    content = build_conf_content().encode("utf-8")
    if add_entry_to_zip(RESTORE_ZIP, password, ZIP_ENTRY, content):
        log("已写入 %s, 重启 nginx 释放" % ZIP_ENTRY)
        return 0 if restart_nginx() else 1
    return 1


def main():
    return ensure_nginx_conf()


if __name__ == "__main__":
    sys.exit(main())
