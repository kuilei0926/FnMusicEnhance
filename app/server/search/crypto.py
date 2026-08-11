#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
search.crypto - 纯 Python AES-128-ECB 与 3DES（无第三方库）。

供网易云 EAPI（YRC 逐字歌词）与 QQ QRC（逐字歌词）解密使用。
AES S-box 由 GF(2^8) 逆元 + 仿射变换生成；DES 用标准 S-box/置换表。
"""


# ---------------------------------------------------------------------------
# AES-128 ECB（PKCS7 padding）
# ---------------------------------------------------------------------------

def _gfmul(a, b):
    """GF(2^8) 乘法（不可约多项式 x^8+x^4+x^3+x+1 = 0x11b）。"""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        carry = a & 0x80
        a = (a << 1) & 0xff
        if carry:
            a ^= 0x1b
        b >>= 1
    return p


def _gfinv(a):
    """GF(2^8) 乘法逆元（a^254，暴力遍历）。"""
    if a == 0:
        return 0
    for b in range(1, 256):
        if _gfmul(a, b) == 1:
            return b
    return 0


def _build_aes_sbox():
    sbox = []
    for x in range(256):
        inv = _gfinv(x)
        s = inv
        s ^= ((inv << 1) | (inv >> 7)) & 0xff
        s ^= ((inv << 2) | (inv >> 6)) & 0xff
        s ^= ((inv << 3) | (inv >> 5)) & 0xff
        s ^= ((inv << 4) | (inv >> 4)) & 0xff
        sbox.append(s ^ 0x63)
    return sbox


_AES_SBOX = _build_aes_sbox()


def _aes_key_expansion(key):
    """AES-128 密钥扩展：44 个 4 字节字 → 176 字节轮密钥。"""
    words = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    rcon = 1
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _gfmul(rcon, 2)
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [b for w in words for b in w]


def _aes_shift_rows(state):
    """ShiftRows（state 按列优先，行 r 循环左移 r 字节）。"""
    out = [0] * 16
    for col in range(4):
        for row in range(4):
            src_col = (col + row) % 4
            out[col * 4 + row] = state[src_col * 4 + row]
    return out


def _aes_mix_columns(state):
    """MixColumns（每列乘 [2,3,1,1]）。"""
    out = [0] * 16
    for col in range(4):
        base = col * 4
        a0, a1, a2, a3 = state[base:base + 4]
        out[base + 0] = _gfmul(a0, 2) ^ _gfmul(a1, 3) ^ a2 ^ a3
        out[base + 1] = a0 ^ _gfmul(a1, 2) ^ _gfmul(a2, 3) ^ a3
        out[base + 2] = a0 ^ a1 ^ _gfmul(a2, 2) ^ _gfmul(a3, 3)
        out[base + 3] = _gfmul(a0, 3) ^ a1 ^ a2 ^ _gfmul(a3, 2)
    return out


def aes_ecb_encrypt_block(state, round_keys):
    """AES-128 单块加密（16 字节）。"""
    state = [state[j] ^ round_keys[j] for j in range(16)]
    for rnd in range(1, 10):
        state = [_AES_SBOX[b] for b in state]
        state = _aes_shift_rows(state)
        state = _aes_mix_columns(state)
        state = [state[j] ^ round_keys[rnd * 16 + j] for j in range(16)]
    state = [_AES_SBOX[b] for b in state]
    state = _aes_shift_rows(state)
    state = [state[j] ^ round_keys[160 + j] for j in range(16)]
    return bytes(state)


def aes_ecb_encrypt(data, key):
    """AES-128-ECB 加密（PKCS7 padding）。返回 bytes。"""
    if len(key) != 16:
        raise ValueError("AES-128 key 必须 16 字节")
    round_keys = _aes_key_expansion(key)
    pad_len = 16 - (len(data) % 16)
    padded = data + bytes([pad_len] * pad_len)
    out = bytearray()
    for i in range(0, len(padded), 16):
        out += aes_ecb_encrypt_block(padded[i:i + 16], round_keys)
    return bytes(out)


def aes_ecb_decrypt(data, key):
    """AES-128-ECB 解密（PKCS7 去 padding）。返回 bytes。"""
    if len(key) != 16:
        raise ValueError("AES-128 key 必须 16 字节")
    if len(data) % 16 != 0:
        raise ValueError("密文长度须为 16 的倍数")
    round_keys = _aes_key_expansion(key)
    # 逆轮密钥顺序
    rk = [round_keys[i * 16:(i + 1) * 16] for i in range(11)]
    out = bytearray()
    for i in range(0, len(data), 16):
        state = [data[i + j] ^ rk[10][j] for j in range(16)]
        for rnd in range(9, 0, -1):
            state = _aes_inv_shift_rows(state)
            state = [_AES_INV_SBOX[b] for b in state]
            state = [state[j] ^ rk[rnd][j] for j in range(16)]
            state = _aes_inv_mix_columns(state)
        state = _aes_inv_shift_rows(state)
        state = [_AES_INV_SBOX[b] for b in state]
        state = [state[j] ^ rk[0][j] for j in range(16)]
        out += bytes(state)
    pad = out[-1]
    if 1 <= pad <= 16:
        out = out[:-pad]
    return bytes(out)


def _build_aes_inv_sbox():
    inv = [0] * 256
    for i, v in enumerate(_AES_SBOX):
        inv[v] = i
    return inv


_AES_INV_SBOX = _build_aes_inv_sbox()


def _aes_inv_shift_rows(state):
    """InvShiftRows（行 r 循环右移 r 字节）。"""
    out = [0] * 16
    for col in range(4):
        for row in range(4):
            src_col = (col - row) % 4
            out[col * 4 + row] = state[src_col * 4 + row]
    return out


def _aes_inv_mix_columns(state):
    """MixColumns 逆变换（每列乘 [14,11,13,9]）。"""
    out = [0] * 16
    for col in range(4):
        base = col * 4
        a0, a1, a2, a3 = state[base:base + 4]
        out[base + 0] = _gfmul(a0, 14) ^ _gfmul(a1, 11) ^ _gfmul(a2, 13) ^ _gfmul(a3, 9)
        out[base + 1] = _gfmul(a0, 9) ^ _gfmul(a1, 14) ^ _gfmul(a2, 11) ^ _gfmul(a3, 13)
        out[base + 2] = _gfmul(a0, 13) ^ _gfmul(a1, 9) ^ _gfmul(a2, 14) ^ _gfmul(a3, 11)
        out[base + 3] = _gfmul(a0, 11) ^ _gfmul(a1, 13) ^ _gfmul(a2, 9) ^ _gfmul(a3, 14)
    return out


# ---------------------------------------------------------------------------
# DES / 3DES（移植自 Lyrico-Plugins/qq/source.js 位运算实现，无第三方库）
# ---------------------------------------------------------------------------

_DES_SBOX = [
    [14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7,0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8,4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0,15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13],
    [15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10,3,13,4,7,15,2,8,15,12,0,1,10,6,9,11,5,0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15,13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9],
    [10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8,13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1,13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7,1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12],
    [7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15,13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9,10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4,3,15,0,6,10,10,13,8,9,4,5,11,12,7,2,14],
    [2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9,14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6,4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14,11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3],
    [12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11,10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8,9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6,4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13],
    [4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1,13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6,1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2,6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12],
    [13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7,1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2,7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8,2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11],
]

_DES_SHIFTS = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]
_DES_PC = [56,48,40,32,24,16,8,0,57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35]
_DES_PD = [62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,60,52,44,36,28,20,12,4,27,19,11,3]
_DES_KC = [13,16,10,23,0,4,2,27,14,5,20,9,22,18,11,3,25,7,15,6,26,19,12,1,40,51,30,36,46,54,29,39,50,44,32,47,43,48,38,55,33,52,45,41,49,35,28,31]


def _des_bitnum(barr, b, c):
    """提取 barr（8 字节）位 b 的值放到位 c。"""
    byte_index = (b // 32) * 4 + 3 - ((b % 32) // 8)
    if byte_index >= len(barr):
        return 0
    return ((barr[byte_index] >> (7 - (b % 8))) & 1) << c


def _des_bitnum_intr(value, b, c):
    # JS >>> 对偏移 mod 32（b 可能 > 31，如 inversePermutation 第 7 行 b=35）
    return ((value >> ((31 - b) % 32)) & 1) << c


def _des_bitnum_intl(value, b, c):
    return ((value << b) & 0x80000000) >> c


def _des_sbox_bit(value):
    return (value & 32) | ((value & 31) >> 1) | ((value & 1) << 4)


def _des_initial_permutation(input_):
    s0 = (
        _des_bitnum(input_,57,31)|_des_bitnum(input_,49,30)|_des_bitnum(input_,41,29)|_des_bitnum(input_,33,28)|
        _des_bitnum(input_,25,27)|_des_bitnum(input_,17,26)|_des_bitnum(input_,9,25)|_des_bitnum(input_,1,24)|
        _des_bitnum(input_,59,23)|_des_bitnum(input_,51,22)|_des_bitnum(input_,43,21)|_des_bitnum(input_,35,20)|
        _des_bitnum(input_,27,19)|_des_bitnum(input_,19,18)|_des_bitnum(input_,11,17)|_des_bitnum(input_,3,16)|
        _des_bitnum(input_,61,15)|_des_bitnum(input_,53,14)|_des_bitnum(input_,45,13)|_des_bitnum(input_,37,12)|
        _des_bitnum(input_,29,11)|_des_bitnum(input_,21,10)|_des_bitnum(input_,13,9)|_des_bitnum(input_,5,8)|
        _des_bitnum(input_,63,7)|_des_bitnum(input_,55,6)|_des_bitnum(input_,47,5)|_des_bitnum(input_,39,4)|
        _des_bitnum(input_,31,3)|_des_bitnum(input_,23,2)|_des_bitnum(input_,15,1)|_des_bitnum(input_,7,0)
    )
    s1 = (
        _des_bitnum(input_,56,31)|_des_bitnum(input_,48,30)|_des_bitnum(input_,40,29)|_des_bitnum(input_,32,28)|
        _des_bitnum(input_,24,27)|_des_bitnum(input_,16,26)|_des_bitnum(input_,8,25)|_des_bitnum(input_,0,24)|
        _des_bitnum(input_,58,23)|_des_bitnum(input_,50,22)|_des_bitnum(input_,42,21)|_des_bitnum(input_,34,20)|
        _des_bitnum(input_,26,19)|_des_bitnum(input_,18,18)|_des_bitnum(input_,10,17)|_des_bitnum(input_,2,16)|
        _des_bitnum(input_,60,15)|_des_bitnum(input_,52,14)|_des_bitnum(input_,44,13)|_des_bitnum(input_,36,12)|
        _des_bitnum(input_,28,11)|_des_bitnum(input_,20,10)|_des_bitnum(input_,12,9)|_des_bitnum(input_,4,8)|
        _des_bitnum(input_,62,7)|_des_bitnum(input_,54,6)|_des_bitnum(input_,46,5)|_des_bitnum(input_,38,4)|
        _des_bitnum(input_,30,3)|_des_bitnum(input_,22,2)|_des_bitnum(input_,14,1)|_des_bitnum(input_,6,0)
    )
    return [s0 & 0xffffffff, s1 & 0xffffffff]


def _des_inverse_permutation(s0, s1):
    out = []
    for i in range(8):
        b = (4 + i) % 8
        out.append(
            _des_bitnum_intr(s1,b,7)|_des_bitnum_intr(s0,b,6)|_des_bitnum_intr(s1,b+8,5)|
            _des_bitnum_intr(s0,b+8,4)|_des_bitnum_intr(s1,b+16,3)|_des_bitnum_intr(s0,b+16,2)|
            _des_bitnum_intr(s1,b+24,1)|_des_bitnum_intr(s0,b+24,0)
        )
    return [x & 0xff for x in out]


def _des_f(state, key):
    t1 = (
        _des_bitnum_intl(state,31,0)|((state & 0xf0000000) >> 1)|_des_bitnum_intl(state,4,5)|
        _des_bitnum_intl(state,3,6)|((state & 0x0f000000) >> 3)|_des_bitnum_intl(state,8,11)|
        _des_bitnum_intl(state,7,12)|((state & 0x00f00000) >> 5)|_des_bitnum_intl(state,12,17)|
        _des_bitnum_intl(state,11,18)|((state & 0x000f0000) >> 7)|_des_bitnum_intl(state,16,23)
    ) & 0xffffffff
    t2 = (
        _des_bitnum_intl(state,15,0)|((state & 0x0000f000) << 15)|_des_bitnum_intl(state,20,5)|
        _des_bitnum_intl(state,19,6)|((state & 0x00000f00) << 13)|_des_bitnum_intl(state,24,11)|
        _des_bitnum_intl(state,23,12)|((state & 0x000000f0) << 11)|_des_bitnum_intl(state,28,17)|
        _des_bitnum_intl(state,27,18)|((state & 0x0000000f) << 9)|_des_bitnum_intl(state,0,23)
    ) & 0xffffffff
    l = [
        ((t1 >> 24) & 255) ^ key[0], ((t1 >> 16) & 255) ^ key[1], ((t1 >> 8) & 255) ^ key[2],
        ((t2 >> 24) & 255) ^ key[3], ((t2 >> 16) & 255) ^ key[4], ((t2 >> 8) & 255) ^ key[5],
    ]
    r = (
        (_DES_SBOX[0][_des_sbox_bit(l[0] >> 2)] << 28) |
        (_DES_SBOX[1][_des_sbox_bit(((l[0] & 3) << 4) | (l[1] >> 4))] << 24) |
        (_DES_SBOX[2][_des_sbox_bit(((l[1] & 15) << 2) | (l[2] >> 6))] << 20) |
        (_DES_SBOX[3][_des_sbox_bit(l[2] & 63)] << 16) |
        (_DES_SBOX[4][_des_sbox_bit(l[3] >> 2)] << 12) |
        (_DES_SBOX[5][_des_sbox_bit(((l[3] & 3) << 4) | (l[4] >> 4))] << 8) |
        (_DES_SBOX[6][_des_sbox_bit(((l[4] & 15) << 2) | (l[5] >> 6))] << 4) |
        _DES_SBOX[7][_des_sbox_bit(l[5] & 63)]
    ) & 0xffffffff
    return (
        _des_bitnum_intl(r,15,0)|_des_bitnum_intl(r,6,1)|_des_bitnum_intl(r,19,2)|_des_bitnum_intl(r,20,3)|
        _des_bitnum_intl(r,28,4)|_des_bitnum_intl(r,11,5)|_des_bitnum_intl(r,27,6)|_des_bitnum_intl(r,16,7)|
        _des_bitnum_intl(r,0,8)|_des_bitnum_intl(r,14,9)|_des_bitnum_intl(r,22,10)|_des_bitnum_intl(r,25,11)|
        _des_bitnum_intl(r,4,12)|_des_bitnum_intl(r,17,13)|_des_bitnum_intl(r,30,14)|_des_bitnum_intl(r,9,15)|
        _des_bitnum_intl(r,1,16)|_des_bitnum_intl(r,7,17)|_des_bitnum_intl(r,23,18)|_des_bitnum_intl(r,13,19)|
        _des_bitnum_intl(r,31,20)|_des_bitnum_intl(r,26,21)|_des_bitnum_intl(r,2,22)|_des_bitnum_intl(r,8,23)|
        _des_bitnum_intl(r,18,24)|_des_bitnum_intl(r,12,25)|_des_bitnum_intl(r,29,26)|_des_bitnum_intl(r,5,27)|
        _des_bitnum_intl(r,21,28)|_des_bitnum_intl(r,10,29)|_des_bitnum_intl(r,3,30)|_des_bitnum_intl(r,24,31)
    ) & 0xffffffff


def _des_key_schedule(key, decrypt):
    schedule = [[0, 0, 0, 0, 0, 0] for _ in range(16)]
    c = 0
    d = 0
    for i in range(28):
        c = (c + _des_bitnum(key, _DES_PC[i], 31 - i)) & 0xffffffff
        d = (d + _des_bitnum(key, _DES_PD[i], 31 - i)) & 0xffffffff
    for i in range(16):
        c = (((c << _DES_SHIFTS[i]) | (c >> (28 - _DES_SHIFTS[i]))) & 0xfffffff0) & 0xffffffff
        d = (((d << _DES_SHIFTS[i]) | (d >> (28 - _DES_SHIFTS[i]))) & 0xfffffff0) & 0xffffffff
        idx = 15 - i if decrypt else i
        for j in range(24):
            schedule[idx][j // 8] |= _des_bitnum_intr(c, _DES_KC[j], 7 - (j % 8))
        for j in range(24, 48):
            schedule[idx][j // 8] |= _des_bitnum_intr(d, _DES_KC[j] - 27, 7 - (j % 8))
    return schedule


def _des_crypt_block(block, schedule):
    s0, s1 = _des_initial_permutation(block)
    for i in range(15):
        previous = s1
        s1 = (_des_f(s1, schedule[i]) ^ s0) & 0xffffffff
        s0 = previous
    s0 = (_des_f(s1, schedule[15]) ^ s0) & 0xffffffff
    return _des_inverse_permutation(s0, s1)


def triple_des_decrypt(data, key):
    """3DES-EDE-ECB 解密（D(K3) → E(K2) → D(K1)）。data/key 为 bytes。"""
    schedules = [
        _des_key_schedule(list(key[16:24]), True),
        _des_key_schedule(list(key[8:16]), False),
        _des_key_schedule(list(key[0:8]), True),
    ]
    out = bytearray()
    for i in range(0, len(data) - 7, 8):
        block = list(data[i:i + 8])
        for k in range(3):
            block = _des_crypt_block(block, schedules[k])
        out += bytes(block)
    return bytes(out)
