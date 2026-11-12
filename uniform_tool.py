#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
XCOM 2 (WOTC) Iridar's Appearance Manager — Uniform 绑定离线工具

数据来源: 角色池 DefaultCharacterPool.bin 中 CharacterPoolManager_AM 的 ExtraDatas
          (CharacterPoolExtraData.NonSoldierUniformTemplates / UniformStatus)

用法:
  python uniform_tool.py list
        列出所有 Uniform 及其绑定的敌人模板

  python uniform_tool.py report [--out 输出路径]
        生成 HTML 总览（含敌人模板的显示名、来源 mod）

  python uniform_tool.py templates --search cerberus
        在已安装 mod 中搜索敌人模板，查来源 mod

  python uniform_tool.py bind --uniform 机械忍者-黑 --template CerberusNemesisM2
        给 Uniform 追加绑定

  python uniform_tool.py unbind --uniform 机械忍者-黑 --template CerberusNemesisM2
        移除绑定

  python uniform_tool.py status --uniform 机械忍者-黑 --value NonSoldier
        修改 UniformStatus (NotUniform / Manual / AnyClass / ClassSpecific / NonSoldier)

注意:
  * 修改前请完全退出游戏（工具会检测 XCom2.exe 并拒绝写入）。
  * 每次写入前都会备份到本工具目录下的 backups/：
      <角色池名>.original.bin  第一次编辑前的原文件，永久保留
      <角色池名>.<时间戳>.bin  滚动备份，只保留最近 10 份
  * 写入后会重新解析校验，校验失败则不落盘。
"""

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys

POOL_REL = os.path.join("XComGame", "CharacterPool", "DefaultCharacterPool.bin")

STATUS_NAMES = {
    "EUS_NotUniform": "非Uniform",
    "EUS_Manual": "手动",
    "EUS_AnyClass": "全职业",
    "EUS_ClassSpecific": "限定职业",
    "EUS_NonSoldier": "非士兵(敌人)",
}
# 页面下拉的排序（唯一来源，随目录下发给前端）
STATUS_ORDER = ("EUS_NonSoldier", "EUS_AnyClass", "EUS_ClassSpecific",
                "EUS_Manual", "EUS_NotUniform")
STATUS_BY_ARG = {
    "notuniform": "EUS_NotUniform",
    "manual": "EUS_Manual",
    "anyclass": "EUS_AnyClass",
    "classspecific": "EUS_ClassSpecific",
    "nonsoldier": "EUS_NonSoldier",
}

# 本地自定义的 Uniform 星级（1-5，0 表示未评级），只影响本工具的界面，不写入角色池
RATINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "uniform_ratings.json")


def load_ratings(path=None):
    path = path or RATINGS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if str(k).strip().isdigit():
                try:
                    n = int(v)
                except Exception:
                    continue
                if 1 <= n <= 5:
                    out[str(int(k))] = n
    return out


def save_ratings(data, path=None):
    path = path or RATINGS_FILE
    body = {k: data[k] for k in sorted(data, key=lambda x: int(x))}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def stars(n):
    return "★" * int(n) if n else ""


# Uniform 预览图：角色池里没有图像数据，只能由用户上传；存 <对象ID>.<扩展名>
UNIFORM_IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "uniform_images")
IMAGE_NAME_RE = re.compile(r"^(\d+)\.(png|jpg|jpeg|webp|gif)$", re.I)


def load_uniform_images():
    """扫描 uniform_images/，返回 {对象ID: 文件名}。"""
    out = {}
    try:
        for fn in os.listdir(UNIFORM_IMAGE_DIR):
            m = IMAGE_NAME_RE.match(fn)
            if m:
                out[m.group(1)] = fn
    except Exception:
        pass
    return out


def remove_uniform_images(oid):
    try:
        for fn in os.listdir(UNIFORM_IMAGE_DIR):
            m = IMAGE_NAME_RE.match(fn)
            if m and m.group(1) == str(oid):
                os.remove(os.path.join(UNIFORM_IMAGE_DIR, fn))
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 方案2：导出计划（Python 规划 + 游戏内执行）
#   导出两份文件：
#     1) exports/uniform_plan_<时间戳>.json —— 可移植的完整计划
#     2) <游戏目录>/XComGame/Config/XComUniformTool.ini —— UniformToolApply mod 直接读取
#   游戏内由 UniformToolApply mod 通过 AM 的 API 幂等应用，落盘完全走引擎。
# ----------------------------------------------------------------------------

EXPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports")
# ModBuddy 项目名 = UnrealScript 包名；ini 段名必须是「包名.类名」。
# 以后改 mod 名，只需改 MOD_PACKAGE 这一行并重新导出。
MOD_PACKAGE = "Up7UniformToolApply"
MOD_INI_SECTION = MOD_PACKAGE + ".X2DLC_UniformToolApply"
MOD_INI_FILE = "XComUniformTool.ini"


def plan_entries(pool):
    """当前角色池里所有「非默认」Uniform 的目标状态（按 ObjectID 排序）。"""
    out = []
    for u in pool.uniforms():
        status = u["status"] or "EUS_NotUniform"
        if status == "EUS_NotUniform" and not u["templates"]:
            continue
        out.append({
            "object_id": u["object_id"],
            "name": display_name(u),
            "status": status,
            "status_cn": STATUS_NAMES.get(status, status),
            "templates": list(u["templates"]),
        })
    out.sort(key=lambda e: e["object_id"])
    return out


def export_plan(pool_path, json_path=None, ini_path=None):
    p = Pool(pool_path)
    entries = plan_entries(p)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if json_path is None:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        json_path = os.path.join(
            EXPORTS_DIR, "uniform_plan_%s.json"
            % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    if ini_path is None:
        cfg_dir = os.path.join(os.path.dirname(os.path.dirname(pool_path)), "Config")
        os.makedirs(cfg_dir, exist_ok=True)
        ini_path = os.path.join(cfg_dir, MOD_INI_FILE)
    body = {
        "generated": ts, "pool": pool_path,
        "note": "UniformToolApply mod 用；状态与模板为目标值，游戏内幂等应用",
        "uniforms": entries,
    }
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, json_path)
    lines = [
        "; 由 uniform_tool.py 导出，供 UniformToolApply mod 读取",
        "; 生成时间: " + ts,
        "; 重要：请先导出再启动游戏（游戏退出时可能用内存中的配置覆盖本文件）",
        "",
        "[" + MOD_INI_SECTION + "]",
    ]
    for e in entries:
        lines.append('+Plans=(ObjectID=%d,Status="%s",Templates="%s")'
                     % (e["object_id"], e["status"], "|".join(e["templates"])))
    tmp = ini_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, ini_path)
    return {"json": json_path, "ini": ini_path, "count": len(entries),
            "with_templates": sum(1 for e in entries if e["templates"])}


# ----------------------------------------------------------------------------
# 敌人模板筛选预设（AM = Iridar's Appearance Manager，工程内统一缩写）
#   strict  按AM规则（严格）：与游戏内实测清单一致（可编辑敌人模板_补全.txt）
#   approx  按AM规则（近似）：按AM判定逻辑推演，只保留确认可用的
#   usable  全部可用模板：确认可用 + 未确认，排除已判定不可用的
#   all     全部模板
# ----------------------------------------------------------------------------

KNOWN_TPL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "可编辑敌人模板_补全.txt")
# 四档预设的唯一规则表：CLI、报告统计、页面筛选都从这里派生。
#   need_known  只保留游戏内实测清单里的模板
#   avail       允许的可用性三态（avail_any 的返回值：ok / unk / bad）
PRESET_RULES = {
    "strict": {"need_known": True},
    "approx": {"avail": ("ok",)},
    "usable": {"avail": ("ok", "unk")},
    "all": {},
}
PRESETS = tuple(PRESET_RULES)
PRESET_LABELS = {
    "strict": "按AM规则（严格）",
    "approx": "按AM规则（近似）",
    "usable": "全部可用模板",
    "all": "全部模板",
}

_known_cache = None
_known_warned = False


def preset_pass(preset, known=False, avail="ok"):
    """四档预设的唯一判定。"""
    rule = PRESET_RULES[preset]
    if rule.get("need_known") and not known:
        return False
    allowed = rule.get("avail")
    return allowed is None or avail in allowed


def load_known_templates(path=None):
    """读取游戏内实测清单：只取 `  - Name` 这种条目，忽略 ※ 说明行。"""
    global _known_cache, _known_warned
    if path is None and _known_cache is not None:
        return _known_cache
    known = set()
    try:
        with open(path or KNOWN_TPL_FILE, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*-\s+([A-Za-z0-9_-]+)\s*$", line)
                if m:
                    known.add(m.group(1).lower())
    except Exception:
        if path is None and not _known_warned:
            _known_warned = True
            print("警告: 读不到实测模板清单 %s，「按AM规则（严格）」预设将为空。"
                  % KNOWN_TPL_FILE, file=sys.stderr)
    if path is None:
        _known_cache = known
    return known


def avail_any(ent, gender):
    """展示口径的三态合并：both 时任一性别可用即 ok，都没有可用但有未确认则 unk。"""
    gens = (1, 2) if gender == "both" else ((1,) if gender == "male" else (2,))
    res = [availability_reason(ent or {}, g)[0] for g in gens]
    if True in res:
        return "ok"
    if None in res:
        return "unk"
    return "bad"


def tri_any(ent):
    """both 口径的三态串：""=可用、"?原因"=未确认、其它=不可用原因。
    与 avail_any 同一判定，供前端直接使用，避免出现第二套规则。"""
    res = [availability_reason(ent or {}, g) for g in (1, 2)]
    if any(ok is True for ok, _ in res):
        return ""
    for ok, why in res:
        if ok is None:
            return "?" + (why or "未确认")
    return res[0][1] or res[1][1] or "不可用"


BIND_CLASS_LIMITS = (2, 5)  # 与页面共用：0 灰、≤2 绿、≤5 蓝、更多 黄


def bind_class(n):
    """绑定次数配色，阈值见 BIND_CLASS_LIMITS。"""
    n = int(n or 0)
    if n <= 0:
        return "bc0"
    if n <= BIND_CLASS_LIMITS[0]:
        return "bc1"
    if n <= BIND_CLASS_LIMITS[1]:
        return "bc2"
    return "bc3"


PROP_TYPES = (
    "IntProperty", "FloatProperty", "BoolProperty", "StrProperty",
    "NameProperty", "ByteProperty", "StructProperty", "ArrayProperty",
)

# ----------------------------------------------------------------------------
# 二进制读写（UE3 tagged properties）
# ----------------------------------------------------------------------------


def i32(b, o):
    return struct.unpack_from("<i", b, o)[0]


def f32(b, o):
    return struct.unpack_from("<f", b, o)[0]


def read_fstr(b, o):
    n = i32(b, o)
    o += 4
    if n == 0:
        return "", o
    if n < 0:
        raw = b[o:o + (-n) * 2]
        o += (-n) * 2
        if raw.endswith(b"\x00\x00"):
            raw = raw[:-2]
        return raw.decode("utf-16-le", "replace"), o
    raw = b[o:o + n]
    o += n
    if raw.endswith(b"\x00"):
        raw = raw[:-1]
    return raw.decode("utf-8", "replace"), o


def read_fname(b, o):
    s, o = read_fstr(b, o)
    return s, o + 4


def peek_fname(b, o):
    try:
        n = i32(b, o)
    except struct.error:
        return None
    if n == 0:
        return ""
    if n < 0:
        if -n > 256:
            return None
        raw = b[o + 4:o + 4 + (-n) * 2]
        if not raw.endswith(b"\x00\x00"):
            return None
        try:
            return raw[:-2].decode("utf-16-le")
        except Exception:
            return None
    if n > 256:
        return None
    raw = b[o + 4:o + 4 + n]
    if not raw.endswith(b"\x00"):
        return None
    try:
        return raw[:-1].decode("utf-8")
    except Exception:
        return None


def enc_fstr(s):
    try:
        raw = s.encode("ascii")
        return struct.pack("<i", len(raw) + 1) + raw + b"\x00"
    except UnicodeEncodeError:
        raw = s.encode("utf-16-le")
        return struct.pack("<i", -(len(raw) // 2 + 1)) + raw + b"\x00\x00"


def enc_fname(s):
    return enc_fstr(s) + struct.pack("<i", 0)


def read_tag(b, o):
    start = o
    name, o = read_fname(b, o)
    typ, o = read_fname(b, o)
    size = i32(b, o)
    o += 4
    idx = i32(b, o)
    o += 4
    extra = None
    if typ in ("StructProperty", "ByteProperty"):
        extra, o = read_fname(b, o)
    payload = o
    end = o + (1 if typ == "BoolProperty" else size)
    return {
        "start": start, "name": name, "typ": typ, "size": size, "idx": idx,
        "extra": extra, "payload": payload, "end": end,
    }


def tag_stream(b, o):
    props = []
    while True:
        nm = peek_fname(b, o)
        if nm is None:
            raise ValueError("无法解析的属性流，偏移 %d" % o)
        if nm == "None":
            _, o = read_fname(b, o)
            return props, o
        t = read_tag(b, o)
        if t["end"] > len(b):
            raise ValueError("属性 %s 越界" % t["name"])
        props.append(t)
        o = t["end"]


def decode_value(b, t):
    o, s, typ = t["payload"], t["size"], t["typ"]
    if typ == "IntProperty":
        return i32(b, o)
    if typ == "FloatProperty":
        return f32(b, o)
    if typ == "BoolProperty":
        return b[o]
    if typ in ("StrProperty", "NameProperty", "ByteProperty"):
        v, _ = read_fstr(b, o) if typ != "ByteProperty" else read_fname(b, o)
        return v
    if typ == "StructProperty":
        sub, _ = tag_stream_mem(b, o, o + s)
        return {x["name"]: decode_value(b, x) for x in sub}
    if typ == "ArrayProperty":
        return decode_array(b, o, s)
    return b[o:o + s]


def tag_stream_mem(b, o, end):
    props = []
    while o < end:
        nm = peek_fname(b, o)
        if nm == "None":
            _, o = read_fname(b, o)
            break
        t = read_tag(b, o)
        props.append(t)
        o = t["end"]
    return props, o


def decode_array(b, o, size):
    cnt = i32(b, o)
    pos = o + 4
    struct_like = False
    try:
        nm = peek_fname(b, pos)
        if nm and nm != "None":
            t2 = read_tag(b, pos)
            struct_like = t2["typ"] in PROP_TYPES
    except Exception:
        struct_like = False
    out = []
    for _ in range(cnt):
        if struct_like:
            sub, pos = tag_stream_mem(b, pos, o + size)
            out.append({x["name"]: decode_value(b, x) for x in sub})
        else:
            v, pos = read_fname(b, pos)
            out.append(v)
    return out


# ----------------------------------------------------------------------------
# 角色池模型
# ----------------------------------------------------------------------------


class Pool:
    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self.parse()

    def parse(self):
        b = self.data
        self.top, top_end = tag_stream(b, 4)
        extra = None
        for p in self.top:
            if p["name"] == "ExtraDatas":
                extra = p
                break
        if extra is None:
            raise ValueError("角色池里没有 ExtraDatas，确认路径是否正确？")
        self.extra = extra
        self.count = i32(b, extra["payload"])
        pos = extra["payload"] + 4
        self.elems = []
        for i in range(self.count):
            start = pos
            props, pos = tag_stream(b, pos)
            self.elems.append({"index": i, "start": start, "end": pos, "props": props})
        if pos != extra["end"]:
            raise ValueError("ExtraDatas 结束位置不一致 (%d != %d)" % (pos, extra["end"]))
        self.top_end = top_end

    def element_dict(self, el):
        return {p["name"]: decode_value(self.data, p) for p in el["props"]}

    def uniforms(self):
        out = []
        for el in self.elems:
            d = self.element_dict(el)
            cp = d.get("CharPoolData") or {}
            status = d.get("UniformStatus")
            names = d.get("NonSoldierUniformTemplates") or []
            if status in (None, "EUS_NotUniform") and not names:
                continue
            if not str(cp.get("strFirstName") or "").strip():
                if not names:
                    continue
            out.append({
                "index": el["index"],
                "object_id": d.get("ObjectID"),
                "first": cp.get("strFirstName"),
                "last": cp.get("strLastName"),
                "nick": cp.get("strNickName"),
                "class": cp.get("m_SoldierClassTemplateName"),
                "gender": (cp.get("kAppearance") or {}).get("iGender"),
                "status": status or "EUS_NotUniform",
                "templates": list(names),
            })
        return out


def display_name(u):
    nick = u["nick"] or ""
    nick = nick.strip().strip("'").strip('"').strip()
    if nick:
        return nick
    return (u["last"] or "").strip() or ("#%s" % u["object_id"])


# ----------------------------------------------------------------------------
# 写回
# ----------------------------------------------------------------------------


def build_name_array_tag(prop, names):
    payload = struct.pack("<i", len(names)) + b"".join(enc_fname(n) for n in names)
    return (enc_fname(prop) + enc_fname("ArrayProperty") +
            struct.pack("<i", len(payload)) + struct.pack("<i", 0) + payload)


def build_byte_tag(prop, enum_name, value):
    payload = enc_fname(value)
    return (enc_fname(prop) + enc_fname("ByteProperty") +
            struct.pack("<i", len(payload)) + struct.pack("<i", 0) +
            enc_fname(enum_name) + payload)


def rebuild_element(b, el, edits):
    pieces = []
    used = set()
    for p in el["props"]:
        if p["name"] in edits:
            used.add(p["name"])
            nb = edits[p["name"]]
            if nb:
                pieces.append(nb)
        else:
            pieces.append(bytes(b[p["start"]:p["end"]]))
    for k, v in edits.items():
        if k not in used and v:
            pieces.append(v)
    ins = el["props"][-1]["end"] if el["props"] else el["start"]
    pieces.append(bytes(b[ins:el["end"]]))
    out = b"".join(pieces)
    return out, len(out) - (el["end"] - el["start"])


# ----------------------------------------------------------------------------
# 写盘核心保护（详见 维护日志.md）
#   两段核心代码的 SHA-256 锁在 write_core.lock.json；
#   哈希不符时 write_pool 会拒绝写入，防止误改二进制补丁逻辑。
#   有意修改且人工复核通过后，运行: python uniform_tool.py corehash --update
# ----------------------------------------------------------------------------

# >>> WRITE-CORE A
CORE_LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "write_core.lock.json")


def core_digests():
    """返回两段受保护代码的 SHA-256（按文件文本计算，含注释与空行）。"""
    with open(os.path.abspath(__file__), encoding="utf-8") as f:
        src = f.read()
    out = {}
    for tag in ("A", "B"):
        begin = "# >>> WRITE-CORE " + tag
        end = "# <<< WRITE-CORE " + tag
        i, j = src.find(begin), src.find(end)
        if i < 0 or j <= i:
            raise RuntimeError("写盘核心保护标记缺失: %s" % tag)
        out[tag] = hashlib.sha256(src[i:j].encode("utf-8")).hexdigest()
    return out


def check_write_core():
    """写盘前校验核心代码是否被人动过；动过就拒绝写入。"""
    cur = core_digests()
    try:
        with open(CORE_LOCK_FILE, encoding="utf-8") as f:
            lock = json.load(f)
    except Exception:
        raise SystemExit("缺少写盘核心锁 %s。请人工复核后运行:\n"
                         "  python uniform_tool.py corehash --update" % CORE_LOCK_FILE)
    bad = [t for t in ("A", "B") if lock.get(t) != cur[t]]
    if bad:
        raise SystemExit("写盘核心代码（%s 段）与锁定哈希不一致，已拒绝写入。\n"
                         "如果这是意外改动，请恢复备份；确认无误后再运行:\n"
                         "  python uniform_tool.py corehash --update" % "、".join(bad))


ALLOWED_FLAGS = ("AllowedTypeSoldier", "AllowedTypeVIP", "AllowedTypeDarkVIP")


def zero_allowed_flags(data, pool, idx):
    """AM 的 SaveCharacterPool 会把 Uniform（状态>NotUniform）的 bAllowedType*
    全部置 false；这里在 CharPoolData 结构体内同步清零。
    等长改字节（BoolProperty 的 payload 就是 1 个字节），不影响文件布局。"""
    el = pool.elems[idx]
    n = 0
    for t in el["props"]:
        if t["name"] != "CharPoolData" or t["typ"] != "StructProperty":
            continue
        subs, _ = tag_stream_mem(data, t["payload"], t["payload"] + t["size"])
        for s in subs:
            if s["name"] in ALLOWED_FLAGS and s["typ"] == "BoolProperty" \
                    and data[s["payload"]]:
                data[s["payload"]] = 0
                n += 1
    return n


def apply_edits(pool, element_edits, zero_allowed=()):
    """element_edits: {elem_index: {prop_name: new_tag_bytes_or_None}}
       zero_allowed: 需要把 CharPoolData.AllowedType* 清零的条目下标"""
    data = bytearray(pool.data)
    for idx in zero_allowed:
        zero_allowed_flags(data, pool, idx)
    total = 0
    for idx in sorted(element_edits, reverse=True):
        el = pool.elems[idx]
        new_bytes, delta = rebuild_element(bytes(data), el, element_edits[idx])
        data[el["start"]:el["end"]] = new_bytes
        total += delta
    old_size = pool.extra["size"]
    struct.pack_into("<i", data, pool.extra["payload"] - 8, old_size + total)
    return bytes(data)


def _prop_bytes(data, el):
    """属性名 -> (类型, 该 tag 的原始字节)，用于“只允许指定属性变化”的比对。"""
    return {p["name"]: (p["typ"], bytes(data[p["start"]:p["end"]]))
            for p in el["props"]}


def _charpool_parts(data, el):
    """CharPoolData 的子属性序列；AllowedType* 三个布尔只保留占位、不比数值。"""
    parts = []
    for t in el["props"]:
        if t["name"] == "CharPoolData" and t["typ"] == "StructProperty":
            subs, _ = tag_stream_mem(data, t["payload"], t["payload"] + t["size"])
            for s in subs:
                if s["name"] in ALLOWED_FLAGS and s["typ"] == "BoolProperty":
                    parts.append((s["name"], s["typ"], b""))
                else:
                    parts.append((s["name"], s["typ"],
                                  bytes(data[s["start"]:s["end"]])))
    return parts


def verify(old_pool, new_data, expected):
    """重新解析新数据并逐项核对（每次写盘前必跑，失败则不落盘）。

    expected: {elem_index: {"status":..., "templates":[...], "no_allowed": True}}
    校验内容：
      1. ExtraDatas 条目数、位置不变；
      2. 目标条目只改了声明的属性（模板数组 / 状态；no_allowed 时还有 CharPoolData 的 AllowedType*）；
      3. 其它条目的字节完全不变（防止误伤）；
      4. ExtraDatas 之前（文件头）与之后（文件尾）的字节完全不变。
    """
    tmp_path = old_pool.path + ".verify.tmp"
    with open(tmp_path, "wb") as f:
        f.write(new_data)
    try:
        p2 = Pool(tmp_path)
    finally:
        os.remove(tmp_path)
    old = old_pool.data
    if p2.count != old_pool.count:
        raise ValueError("校验失败：ExtraDatas 数量变化 %d -> %d" % (old_pool.count, p2.count))
    if (p2.extra["start"] != old_pool.extra["start"]
            or p2.extra["payload"] != old_pool.extra["payload"]):
        raise ValueError("校验失败：ExtraDatas 位置发生变化")
    # 文件头（ExtraDatas 之前）与尾部必须逐字节不变
    if old[:old_pool.extra["start"]] != new_data[:p2.extra["start"]]:
        raise ValueError("校验失败：ExtraDatas 之前的字节被改动")
    if old[old_pool.extra["end"]:] != new_data[p2.extra["end"]:]:
        raise ValueError("校验失败：ExtraDatas 之后的字节被改动")

    # 未涉及的条目必须一个字节都不差
    for i in range(old_pool.count):
        if i in expected:
            continue
        oe, ne = old_pool.elems[i], p2.elems[i]
        if old[oe["start"]:oe["end"]] != new_data[ne["start"]:ne["end"]]:
            raise ValueError("校验失败：条目 %d 被意外修改" % i)

    for i, exp in expected.items():
        d = p2.element_dict(p2.elems[i])
        got_status = d.get("UniformStatus") or "EUS_NotUniform"
        if "status" in exp and got_status != exp["status"]:
            raise ValueError("校验失败：条目 %d 的 UniformStatus 未写入" % i)
        if "templates" in exp:
            got = d.get("NonSoldierUniformTemplates") or []
            if [x.lower() for x in got] != [x.lower() for x in exp["templates"]]:
                raise ValueError("校验失败：条目 %d 的模板列表不一致" % i)
        if exp.get("no_allowed"):
            cp = d.get("CharPoolData") or {}
            for k in ALLOWED_FLAGS:
                if cp.get(k):
                    raise ValueError("校验失败：条目 %d 的 %s 未清零" % (i, k))

        # 目标条目内部：除声明属性外，其余属性字节必须完全一致
        allowed = {"NonSoldierUniformTemplates", "UniformStatus"}
        if exp.get("no_allowed"):
            allowed.add("CharPoolData")
        ob = _prop_bytes(old, old_pool.elems[i])
        nb = _prop_bytes(new_data, p2.elems[i])
        for name in set(ob) | set(nb):
            if name not in allowed and ob.get(name) != nb.get(name):
                raise ValueError("校验失败：条目 %d 的属性 %s 被意外修改" % (i, name))
        if exp.get("no_allowed") and \
                _charpool_parts(old, old_pool.elems[i]) != _charpool_parts(new_data, p2.elems[i]):
            raise ValueError("校验失败：条目 %d 的 CharPoolData 除 AllowedType* 外有改动" % i)
    return p2


def game_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq XCom2.exe", "/NH"],
                             capture_output=True, text=True, timeout=15)
        return "XCom2.exe" in (out.stdout or "")
    except Exception:
        return False


BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
BACKUP_KEEP = 10


def backup(path):
    """备份到工具目录 backups/ 下：
       <名称>.original.bin   第一次编辑前的原文件，永久保留
       <名称>.<时间戳>.bin   滚动备份，只保留最近 BACKUP_KEEP 份
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    original = os.path.join(BACKUP_DIR, base + ".original.bin")
    if not os.path.exists(original):
        shutil.copy2(path, original)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(BACKUP_DIR, "%s.%s.bin" % (base, ts))
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(BACKUP_DIR, "%s.%s_%d.bin" % (base, ts, n))
        n += 1
    shutil.copy2(path, dst)
    rolling = [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
               if f.startswith(base + ".") and not f.endswith(".original.bin")]
    rolling.sort(key=lambda x: (os.path.getmtime(x), x))
    for old in rolling[:max(0, len(rolling) - BACKUP_KEEP)]:
        try:
            os.remove(old)
        except OSError:
            pass
    return dst


def write_pool(pool, new_data, expected):
    check_write_core()
    if game_running():
        raise SystemExit("检测到 XCom2.exe 正在运行。请完全退出游戏后再修改，"
                         "否则游戏退出时会覆盖你的改动。")
    bak = backup(pool.path)
    tmp = pool.path + ".new.tmp"
    with open(tmp, "wb") as f:
        f.write(new_data)
    os.replace(tmp, pool.path)
    return bak
# <<< WRITE-CORE A


# ----------------------------------------------------------------------------
# 已安装 mod 扫描（敌人模板 -> 显示名 / 来源 mod）
# ----------------------------------------------------------------------------


def default_pool_path():
    return os.path.join(os.path.expanduser("~"), "Documents", "My Games",
                        "XCOM2 War of the Chosen", POOL_REL)


def guess_workshop_dirs():
    cands = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = winreg.QueryValueEx(k, "SteamPath")[0]
        cands.append(os.path.join(steam, "steamapps", "workshop", "content", "268500"))
    except Exception:
        pass
    for p in (r"C:\Program Files (x86)\Steam", r"D:\Steam", r"E:\Steam",
              r"D:\Program Files (x86)\Steam"):
        cands.append(os.path.join(p, "steamapps", "workshop", "content", "268500"))
    return [p for p in cands if os.path.isdir(p)]


def guess_base_config_dir(workshop_dir):
    """从 ...\\steamapps\\workshop\\content\\268500 推导游戏本体的 Config 目录"""
    if not workshop_dir:
        return None
    p = workshop_dir
    for _ in range(3):  # content -> workshop -> steamapps
        p = os.path.dirname(p)
    common = os.path.join(p, "common")
    cand = os.path.join(common, "XCOM 2", "XCom2-WarOfTheChosen", "XComGame", "Config")
    if os.path.isdir(cand):
        return cand
    if os.path.isdir(common):
        for name in os.listdir(common):
            cand = os.path.join(common, name, "XCom2-WarOfTheChosen", "XComGame", "Config")
            if os.path.isdir(cand):
                return cand
    return None


def guess_local_mod_dirs():
    root = os.path.join(os.path.expanduser("~"), "Documents", "My Games",
                        "XCOM2 War of the Chosen", "XComGame", "Mods")
    return [root] if os.path.isdir(root) else []


def _read_text(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None
    try:
        return raw.decode("utf-8-sig")
    except Exception:
        return None


def _mod_title(folder):
    try:
        for fn in os.listdir(folder):
            if fn.lower().endswith(".xcommod"):
                t = _read_text(os.path.join(folder, fn)) or ""
                m = re.search(r"^Title=(.+)$", t, re.M)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


SEC_RE = re.compile(r"^\[([^\s\]]+)\s+X2CharacterTemplate\]")
REF_RE = re.compile(r'CharacterTemplate\s*=\s*"?([A-Za-z0-9_\-]+)"?')
NEWCH_RE = re.compile(r'newCharacters\s*=\s*"([A-Za-z0-9_\-]+)"')
TPL_RE = re.compile(r"TemplateName\s*=\s*\"([^\"]+)\"", re.I)
DISP_RE = re.compile(r'strCharacterName\s*=\s*"([^"]*)"', re.I)

# 可用性判断：X2CharacterTemplate 的关键标志（ini 或 mod 源码里）
# nnEnemy 系列把 +Alien/+Soldier/+height 写在 [Mod.X2Character_nnEnemy] 配置段，
# 同时也要认 bIsAlien 这类完整字段名。
FLAG_TRUE_RE = re.compile(
    r'(bIsAlien|Alien|bIsAdvent|Advent|bIsSoldier|Soldier|bIsCosmetic|Cosmetic'
    r'|bNeverSelectable|NeverSelectable|bIsRobotic|Robotic)\s*=\s*(true|1)\b', re.I)
FLAG_MAP = {
    "bisalien": "alien", "alien": "alien", "bisadvent": "advent", "advent": "advent",
    "bissoldier": "soldier", "soldier": "soldier",
    "biscosmetic": "cosmetic", "cosmetic": "cosmetic",
    "bneverselectable": "never", "neverselectable": "never",
    "bisrobotic": "robotic", "robotic": "robotic",
}
APP_RE = re.compile(r"bAppearanceDefinesPawn\s*=\s*(true|false|1|0)\b", re.I)
# 难度数值覆盖段：[{BaseTemplateName}_Diff_{Difficulty} X2CharacterTemplate]
# 这类段只改数值，不会创建模板，不能算作 defined
DIFF_SEC_RE = re.compile(r"_Diff_\d+$", re.I)
SIZE_RE = re.compile(r"\b(?:Unit)?Size\s*=\s*(\d+)", re.I)
HEIGHT_RE = re.compile(r"\b(?:Unit)?Height\s*=\s*(\d+)", re.I)
GROUP_RE = re.compile(r"CharacterGroupName\s*=\s*['\"]([^'\"]*)['\"]", re.I)
# Pawn 配置（XComContent.ini / DefaultContent.ini）：
# +BodyPartTemplateConfig=(PartType="Pawn", ..., ArchetypeName="...", Gender=eGender_Male, CharacterTemplate="X")
PAWN_PART_RE = re.compile(r'PartType\s*=\s*"Pawn"', re.I)
PAWN_CT_RE = re.compile(r'CharacterTemplate\s*=\s*"([^"]+)"', re.I)
PAWN_G_RE = re.compile(r'Gender\s*=\s*(eGender_[A-Za-z]+|\d+)', re.I)
PAWN_ARCH_RE = re.compile(r'ArchetypeName\s*=\s*"([^"]+)"', re.I)
GENDER_MAP = {"egender_none": 0, "egender_male": 1, "egender_female": 2,
              "0": 0, "1": 1, "2": 2}
# UC：CharTemplate.strPawnArchetypes.AddItem("...") / strPawnArchetypes[i] = "..."
ARCH_ADD_RE = re.compile(r'strPawnArchetypes\s*\.\s*Add(?:Item)?\s*\(\s*"([^"]+)"', re.I)
ARCH_SET_RE = re.compile(r'strPawnArchetypes\s*\[\s*(\d+)\s*\]\s*=\s*"([^"]+)"', re.I)
UC_BLOCK_RE = re.compile(
    r"\n\s*(?:static\s+)?(?:private\s+|protected\s+|simulated\s+)*function\s+", re.I)
UC_NAME_RES = (
    re.compile(r"CREATE_X2CHARACTER_TEMPLATE\(\s*[A-Za-z_][A-Za-z0-9_]*\s*,\s*'([^']+)'"),
    re.compile(r"\bCreate[A-Za-z_]*\s*\(\s*'([^']+)'"),
    re.compile(r"SetTemplateName\s*\(\s*'([^']+)'"),
    re.compile(r"\bnew\s*\([^)]*\bstring\s*\(\s*'([^']+)'"),
    re.compile(r"DataName\s*=\s*'([^']+)'"),
)

# 本体里已知的非人形单位（外星人/机器人/僵尸等），游戏内选择列表不会出现
BASE_BAD_STEMS = {
    "sectoid", "muton", "viper", "archon", "berserker", "chryssalid",
    "chryssalidcocoon", "chryssalidcocoonhuman", "cyberus", "faceless",
    "gatekeeper", "sectopod", "prototypesectopod", "andromedon",
    "andromedonrobot", "spectre", "advmec", "advturret", "advshortturret",
    "xcomturret", "adventtrooptransport", "testdummy", "thelost",
    "thelostdasher", "thelosthowler", "chosenassassin", "chosensniper",
    "chosenwarlock", "psizombie", "psizombiehuman", "soldier",
    "reapersoldier", "skirmishersoldier", "templarsoldier",
}
STEM_SUFFIX_RE = re.compile(r"(_?EM\d+|_?M\d+|_?Diff_\w+|MP|HP\d+)$", re.I)


def _ini_lines(text):
    """去注释、合并以 \\ 结尾的续行（BodyPartTemplateConfig 常被拆成多行）。"""
    out, buf = [], ""
    for raw in text.splitlines():
        s = raw.split(";", 1)[0].rstrip()
        if s.endswith("\\"):
            buf += " " + s[:-1].strip()
            continue
        s = (buf + " " + s).strip()
        buf = ""
        if s:
            out.append(s)
    if buf.strip():
        out.append(buf.strip())
    return out


def _parse_flags(text):
    f = {}
    for m in FLAG_TRUE_RE.finditer(text):
        k = FLAG_MAP.get(m.group(1).lower())
        if k:
            f[k] = 1
    m = APP_RE.search(text)
    if m:
        f["app"] = m.group(1).lower() in ("true", "1")
    m = SIZE_RE.search(text)
    if m:
        f["size"] = int(m.group(1))
    m = HEIGHT_RE.search(text)
    if m:
        f["height"] = int(m.group(1))
    m = GROUP_RE.search(text)
    if m:
        f["group"] = m.group(1)
    return f


def template_bad_reason(ent):
    # 注意：AM 的过滤条件只包括 bIsSoldier/bIsCosmetic/bNeverSelectable/bIsAlien/
    # UnitSize/UnitHeight/CharacterGroupName；bIsRobotic 不参与过滤
    # （Heretic Geth 就是 bIsRobotic=true 但仍出现在可选列表里）。
    f = ent.get("flags") or {}
    if f.get("alien"):
        return "外星人"
    if f.get("soldier"):
        return "士兵"
    if f.get("cosmetic"):
        return "仅装饰"
    if f.get("never"):
        return "不可选"
    if f.get("size") not in (None, 1):
        return "体型非标准"
    if f.get("height") not in (None, 2):
        return "高度非标准"
    grp = str(f.get("group") or "").lower()
    if grp in ("speaker", "psizombie"):
        return "特殊单位组"
    if STEM_SUFFIX_RE.sub("", ent.get("template") or "").lower() in BASE_BAD_STEMS:
        return "本体非人形单位"
    return None


# Pawn archetype 分类：True=人形(XComHumanPawn)、False=非人形、None=未确认
ALIEN_ARCH_TOKENS = (
    "viper", "sectoid", "muton", "archon", "berserker", "chryssalid",
    "cyberus", "faceless", "gatekeeper", "sectopod", "andromedon",
    "spectre", "psizombie", "chosen", "thelost", "gremlin", "advmec",
    "scaledprime", "geth", "drone", "turret", "xcomturret",
)
HUMAN_ARCH_TOKENS = (
    "soldier", "civilian", "advcaptain", "cultist", "marauder",
    "missinganims", "dollhouse", "skirmisher", "templar", "reaper",
    "stasis", "headengineer", "headscientist",
)
GENDER_NAMES = {1: "男", 2: "女"}


def arch_kind(arch):
    """Pawn archetype 是否人形。只认本地能确认的证据，未知返回 None。"""
    a = str(arch or "").lower()
    if not a:
        return None
    # 名字里带 Human/Humanoid 的一定是 XComHumanPawn（含 MecHumanHostile、
    # GenericRobotHumanoidHostile；后者是 Heretic Geth 用的，实测在可选列表里）
    if "human" in a:
        return True
    for t in ALIEN_ARCH_TOKENS:
        if t in a:
            return False
    for t in HUMAN_ARCH_TOKENS:
        if t in a:
            return True
    return None


def _pawn_arch_for(ent, gender):
    """按 AM 的规则近似还原 GetPawnArchetypeString 取到的 archetype。"""
    archs = ent.get("archs") or []
    if not archs:
        return None
    if len(archs) == 1:
        return archs[0]
    i = (gender or 1) - 1
    return archs[i] if 0 <= i < len(archs) else None


def availability_reason(ent, gender=1):
    """Iridar's Appearance Manager 的可用性判定（按被编辑单位的性别）。

    返回 (True|False|None, 原因)。None 表示本地证据不足、无法确认。
    """
    bad = template_bad_reason(ent)
    if bad:
        return False, bad
    f = ent.get("flags") or {}
    pawns = ent.get("pawns") or []
    app = f.get("app")

    def from_archs():
        a = _pawn_arch_for(ent, gender)
        if not a:
            return None, "无 Pawn 配置，未确认"
        kind = arch_kind(a)
        if kind is True:
            return True, ""
        if kind is False:
            return False, "Pawn 非人形（%s）" % a
        return None, "未确认 Pawn 类型（%s）" % a

    mine = [p for p in pawns if (p.get("g") or 0) == gender]
    if mine:
        arched = [p.get("arch") for p in mine if p.get("arch")]
        for p in mine:
            if arch_kind(p.get("arch")) is True:
                return True, ""
        if arched:
            return False, "Pawn 非人形（%s）" % arched[0]
    if app is True and pawns:
        # bAppearanceDefinesPawn=true：必须有同性别 Pawn，否则回落到 strPawnArchetypes
        if ent.get("archs"):
            return from_archs()
        return False, "缺少%s性 Pawn 配置" % GENDER_NAMES.get(gender, "")
    # bAppearanceDefinesPawn=false（或未知）：直接按性别取 strPawnArchetypes
    return from_archs()


# 索引缓存结构：字段集合变了，缓存自动失效（不再靠人工递增版本号）
INDEX_FIELDS = ("template", "display", "mods", "defined", "flags",
                "mod_titles", "pawns", "archs", "bad")


def index_signature():
    """索引结构签名：由 INDEX_FIELDS 派生，改结构即换签名。"""
    return hashlib.sha256("|".join(INDEX_FIELDS).encode("utf-8")).hexdigest()[:16]


def _source_state(workshop_dir):
    """workshop 目录指纹：子目录数 + 目录与各子目录的最新 mtime。"""
    if not workshop_dir or not os.path.isdir(workshop_dir):
        return None
    try:
        subs = [x for x in os.listdir(workshop_dir)
                if os.path.isdir(os.path.join(workshop_dir, x))]
    except OSError:
        return None
    stamps = []
    for name in subs:
        try:
            stamps.append(os.path.getmtime(os.path.join(workshop_dir, name)))
        except OSError:
            pass
    try:
        stamps.append(os.path.getmtime(workshop_dir))
    except OSError:
        pass
    return {"dir": os.path.abspath(workshop_dir), "count": len(subs),
            "mtime": round(max(stamps), 3) if stamps else 0}


def build_mod_index(workshop_dir, cache_path, refresh=False, verbose=False):
    state = _source_state(workshop_dir)
    if not refresh and os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            if isinstance(cached, dict) \
                    and cached.get("_version") == index_signature() \
                    and cached.get("_source") == state \
                    and isinstance(cached.get("index"), dict):
                return cached["index"]
            if verbose:
                print("模板索引缓存已过期（mod 目录有变动或结构升级），正在重建…",
                      file=sys.stderr)
        except Exception:
            pass
    index = {}
    proto = {}       # mid -> 该 mod 的模板原型标志（来自 CreateTemplate_xxx 里 new(string(...)) 的批量创建函数）
    newch_mods = {}  # 模板名(小写) -> {通过 newCharacters 定义它的 mid}

    def rec(name):
        k = name.lower()
        d = index.get(k)
        if d is None:
            d = {"template": name, "display": None, "mods": [], "defined": False}
            index[k] = d
        return d

    def scan_file(fp, mid, title):
        t = _read_text(fp)
        if not t or ("CharacterTemplate" not in t and "X2CharacterTemplate" not in t
                     and "newCharacters" not in t):
            return
        cur = None
        cfg_flags, cfg_names, in_cfg = {}, [], False

        def flush_cfg():
            # [Mod.X2Character_nnEnemy] 段里的 +Alien/+size/+height 等字段，
            # 通过段内的 newCharacters 传给对应模板
            for name in cfg_names:
                d = rec(name)
                if cfg_flags:
                    d.setdefault("flags", {}).update(cfg_flags)

        for s in _ini_lines(t):
            if s.startswith("["):
                flush_cfg()
                cfg_flags, cfg_names, in_cfg = {}, [], False
                m = SEC_RE.match(s)
                cur = m.group(1) if m else None
                if cur and DIFF_SEC_RE.search(cur):
                    cur = None  # 只改数值的难度覆盖段，不是模板定义
                elif cur:
                    d = rec(cur)
                    d["defined"] = True
                    if mid not in d["mods"]:
                        d["mods"].append(mid)
                    if title:
                        d.setdefault("mod_titles", {})[mid] = title
                else:
                    in_cfg = bool(re.search(r"X2Character", s))
                continue
            if PAWN_PART_RE.search(s):
                mct = PAWN_CT_RE.search(s)
                if mct:
                    g = 0
                    mg = PAWN_G_RE.search(s)
                    if mg:
                        g = GENDER_MAP.get(mg.group(1).lower(), 0)
                    ma = PAWN_ARCH_RE.search(s)
                    d = rec(mct.group(1))
                    if mid not in d["mods"]:
                        d["mods"].append(mid)
                    if title:
                        d.setdefault("mod_titles", {})[mid] = title
                    if ma:
                        p = {"g": g, "arch": ma.group(1)}
                        lst = d.setdefault("pawns", [])
                        if p not in lst:
                            lst.append(p)
            if cur:
                d = index[cur.lower()]
                m = DISP_RE.search(s)
                if m and not d["display"]:
                    d["display"] = m.group(1)
                f = _parse_flags(s)
                if f:
                    d.setdefault("flags", {}).update(f)
            elif in_cfg:
                cfg_flags.update(_parse_flags(s))
            for m in REF_RE.finditer(s):
                name = m.group(1)
                if len(name) < 3:
                    continue
                d = rec(name)
                if mid not in d["mods"]:
                    d["mods"].append(mid)
                if title:
                    d.setdefault("mod_titles", {})[mid] = title
            for m in NEWCH_RE.finditer(s):
                # XComSetup.ini 的 newCharacters 是 mod 通过代码创建的敌人模板名
                name = m.group(1)
                d = rec(name)
                d["defined"] = True
                if mid not in d["mods"]:
                    d["mods"].append(mid)
                if title:
                    d.setdefault("mod_titles", {})[mid] = title
                newch_mods.setdefault(name.lower(), set()).add(mid)
                if in_cfg:
                    cfg_names.append(name)
        flush_cfg()

    def scan_uc(fp, mid, title):
        """mod 源码里用代码创建 / 修改 X2CharacterTemplate 的模板名与标志"""
        t = _read_text(fp)
        if not t or "X2CharacterTemplate" not in t:
            return
        t = "\n".join(line.split("//", 1)[0] for line in t.splitlines())  # 去注释
        if ("CREATE_X2CHARACTER_TEMPLATE" not in t and "CreateTemplate" not in t
                and "DataName" not in t):
            return
        blocks = UC_BLOCK_RE.split(t)[1:]
        # 第一遍：记录每个函数块自身的标志，供按名字调用的 helper 继承
        func_flags = {}
        parsed = []
        for blk in blocks:
            fn = re.match(r"(?:[A-Za-z_][\w<>.]*\s+)?([A-Za-z_]\w*)\s*\(", blk)
            fname = fn.group(1) if fn else ""
            f = _parse_flags(blk)
            if f and fname:
                func_flags.setdefault(fname, {}).update(f)
            parsed.append((fname, blk))
        for fname, blk in parsed:
            # nnEnemy 系列用 CreateTemplate_newEnemy(default.newCharacters[i]) 批量创建，
            # 函数里的 bAppearanceDefinesPawn/UnitSize 等要作为该 mod 的“模板原型”保留
            f = _parse_flags(blk)
            if f and re.search(r"new\s*\([^)]*string\s*\(", blk):
                proto.setdefault(mid, {}).update(f)
            if "CharacterTemplate" not in blk and "CreateTemplate" not in blk:
                continue
            names = []
            for rx in UC_NAME_RES:
                for m in rx.finditer(blk):
                    if m.group(1) not in names:
                        names.append(m.group(1))
            if not names:
                continue
            archs = []
            for m in ARCH_ADD_RE.finditer(blk):
                archs.append(m.group(1))
            for m in ARCH_SET_RE.finditer(blk):
                i = int(m.group(1))
                while len(archs) <= i:
                    archs.append("")
                archs[i] = m.group(2)
            for name in names:
                d = rec(name)
                d["defined"] = True
                if mid not in d["mods"]:
                    d["mods"].append(mid)
                if title:
                    d.setdefault("mod_titles", {})[mid] = title
                flags = dict(f)
                # 名字若是通过 CreateTemplate_Xxx('Name') 传入的，继承该 helper 的标志
                for m in re.finditer(r"\b(Create[A-Za-z_]*)\s*\(\s*'%s'" % re.escape(name), blk):
                    flags.update(func_flags.get(m.group(1), {}))
                if flags:
                    d.setdefault("flags", {}).update(flags)
                if archs and not d.get("archs"):
                    d["archs"] = archs

    sources = []  # (mid, 标题, 目录)
    if workshop_dir and os.path.isdir(workshop_dir):
        for mid in sorted(x for x in os.listdir(workshop_dir)
                          if os.path.isdir(os.path.join(workshop_dir, x))):
            folder = os.path.join(workshop_dir, mid)
            sources.append((mid, _mod_title(folder), folder))
    base = guess_base_config_dir(workshop_dir)
    if base:
        sources.append(("base", "游戏本体 (XCOM 2 WOTC)", base))
    for root in guess_local_mod_dirs():
        for name in sorted(os.listdir(root)):
            folder = os.path.join(root, name)
            if os.path.isdir(folder):
                sources.append((name, _mod_title(folder), folder))
    if verbose:
        print("扫描 %d 个来源（工坊 %s + 本体 %s + 本地 mod %s）..." % (
            len(sources), "有" if workshop_dir else "无",
            "有" if base else "无",
            "有" if guess_local_mod_dirs() else "无"), file=sys.stderr)
    for mid, title, folder in sources:
        for dp, _, fs in os.walk(folder):
            for fn in fs:
                low = fn.lower()
                if low.endswith(".ini"):
                    scan_file(os.path.join(dp, fn), mid, title)
                elif low.endswith(".uc"):
                    scan_uc(os.path.join(dp, fn), mid, title)
    # newCharacters 定义的模板，自身没有 ini 标志，需继承同 mod 的模板原型标志
    for k, mids in newch_mods.items():
        ent = index.get(k)
        if not ent:
            continue
        f = ent.setdefault("flags", {})
        for mid in mids:
            p = proto.get(mid) or {}
            for key in ("app", "size", "height"):
                if key in p and key not in f:
                    f[key] = p[key]
    for ent in index.values():
        bad = template_bad_reason(ent)
        if bad:
            ent["bad"] = bad
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump({"_version": index_signature(), "_source": state,
                       "index": index}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass
    return index


def pretty_template(name):
    """模板名 -> 便于阅读的名字（去掉 M1/M2/_Diff_N 之类变体后缀，并拆分驼峰）"""
    stem = re.sub(r"(_?EM\d+|_?M\d+|_?Diff_\d+)$", "", name)
    stem = re.sub(r"(_\d+)$", "", stem)
    out = []
    for i, ch in enumerate(stem):
        if i and ch.isupper():
            prev = stem[i - 1]
            nxt = stem[i + 1] if i + 1 < len(stem) else ""
            if prev.islower() or (prev.isupper() and nxt.islower()):
                out.append(" ")
        out.append(ch)
    s = "".join(out).replace("_", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s or name


def source_label(index, tpl):
    ent = index.get(tpl.lower())
    if not ent:
        return None
    titles = ent.get("mod_titles") or {}
    for mid in ent["mods"]:
        if titles.get(mid):
            # 工坊 mod 用数字 ID，本地 mod / 本体用标题
            return ("%s (#%s)" % (titles[mid], mid)) if mid.isdigit() else titles[mid]
    if ent["mods"]:
        return "#" + ent["mods"][0]
    return None


# ----------------------------------------------------------------------------
# 命令：list
# ----------------------------------------------------------------------------


def cmd_list(pool_path, only_bound=True):
    p = Pool(pool_path)
    us = p.uniforms()
    print("角色池: %s" % pool_path)
    print("ExtraDatas: %d 条，其中 Uniform: %d 个\n" % (p.count, len(us)))
    total = 0
    for u in us:
        n = len(u["templates"])
        total += n
        print("#%d %-28s %-9s %-4s %s" % (
            u["object_id"], display_name(u)[:28], STATUS_NAMES.get(u["status"], u["status"]),
            "F" if u["gender"] == 2 else ("M" if u["gender"] == 1 else "?"),
            "%d 个模板" % n if n else "（未绑定敌人）"))
        for t in u["templates"]:
            print("      - %s" % t)
    print("\n合计绑定: %d 条" % total)


# ----------------------------------------------------------------------------
# 命令：report
# ----------------------------------------------------------------------------

EDITOR_HTML = """
<dialog id="dlg">
  <div class="dlg-head"><h3 id="dlgTitle">编辑绑定</h3><button id="dlgClose">✕</button></div>
  <div class="dlg-body">
    <div class="sel">
      <div class="sel-h">已选 <b id="selN">0</b> 项</div>
      <div class="sel-list" id="selList"></div>
    </div>
    <input id="dlgFilter" placeholder="筛选模板 / Uniform / 来源 mod…">
    <div class="seg" id="dlgPreset" title="按当前 Uniform 的性别统计">
      <button data-p="strict" class="on">按AM规则（严格）<i class="pc"></i></button>
      <button data-p="approx">按AM规则（近似）<i class="pc"></i></button>
      <button data-p="usable">全部可用模板<i class="pc"></i></button>
      <button data-p="all">全部模板<i class="pc"></i></button>
    </div>
    <span class="hint" id="dlgPresetHint"></span>
    <div id="dlgGroups" class="pgroups"></div>
  </div>
  <div class="dlg-foot">
    <span class="msg"><span id="dlgMsg"></span>已选 <b id="dlgCount">0</b> 个</span>
    <button id="dlgSave">保存并写入</button>
  </div>
</dialog>
<script type="application/json" id="cat">{{CATALOG}}</script>
<script>
(function(){
  const catEl=document.getElementById('cat');
  if(!catEl) return;
  const CAT=JSON.parse(catEl.textContent);
  const dlg=document.getElementById('dlg');
  const groupsBox=document.getElementById('dlgGroups');
  const filter=document.getElementById('dlgFilter');
  const presetBox=document.getElementById('dlgPreset');
  const presetHint=document.getElementById('dlgPresetHint');
  const countEl=document.getElementById('dlgCount');
  const selN=document.getElementById('selN');
  const selList=document.getElementById('selList');
  const msg=document.getElementById('dlgMsg');
  const saveBtn=document.getElementById('dlgSave');
  // 状态元数据、四档预设规则、配色阈值都由 Python 下发，前端不再手写副本
  const ST={}, ST_ORDER=(CAT.st||[]).map(x=>{ ST[x.k]=x.d; return x.d; });
  const BCT=CAT.bc||[2,5];
  const PR=CAT.pr||{};
  const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  let mode='uniform', curIdx=-1, curTpl='', status='', bindMap={};
  let chosen=[], chosenLow=new Set(), chosenU=new Set();
  let useG=0;  // 当前 Uniform 的性别：0=未指定(男女都算) 1=男 2=女
  let preset='strict';

  function buildBindMap(){
    const m={};
    document.querySelectorAll('.row[data-idx]').forEach(r=>{
      let t=[]; try{ t=JSON.parse(r.dataset.templates||'[]'); }catch(e){}
      t.forEach(x=>(m[x.toLowerCase()]=m[x.toLowerCase()]||[]).push(+r.dataset.idx));
    });
    return m;
  }
  function isOn(o){
    return mode==='uniform' ? chosenLow.has(String(o.n||'').toLowerCase()) : chosenU.has(o.i);
  }
  // o.bm / o.bf: ""=进游戏可用、"?原因"=未确认、其它=不可用原因
  // o.bo 由 Python 按 both 口径算好（与表格 data-avail 同一函数），这里不再自行合并
  function availOf(o){
    if(mode!=='uniform'||!o) return {bad:false,unk:false,why:''};
    const r = useG===1 ? (o.bm||'') : useG===2 ? (o.bf||'') : (o.bo||'');
    if(!r) return {bad:false,unk:false,why:''};
    if(r.charAt(0)==='?') return {bad:false,unk:true,why:r.slice(1)};
    return {bad:true,unk:false,why:r};
  }
  // 四档预设：规则来自 Python 的 PRESET_RULES（CAT.pr），这里只做判定
  function passPreset(o){
    if(mode!=='uniform'||!o) return true;
    const r=PR[preset]||{};
    if(r.need_known&&!o.g) return false;
    if(!r.avail) return true;
    const av=availOf(o);
    return r.avail.indexOf(av.bad?'bad':(av.unk?'unk':'ok'))>=0;
  }
  function bcCls(n){
    n=+(n||0);
    return n<=0?'bc0':(n<=BCT[0]?'bc1':(n<=BCT[1]?'bc2':'bc3'));
  }
  function pill(o){
    const on=isOn(o), av=availOf(o);
    const key=mode==='uniform'?o.n:o.i;
    const sub=mode==='uniform'?o.n:('#'+o.oid);
    let tip=(o.n||o.d)+'  ·  来源: '+(o.s||'未知');
    if(av.bad) tip+='  ·  游戏内不可用: '+av.why;
    else if(av.unk) tip+='  ·  未确认: '+av.why;
    tip+='  ·  '+(mode==='uniform'?'已绑定 '+ (o.c||0) +' 个 Uniform':'已绑定 '+ (o.c||0) +' 个敌人模板');
    if(mode!=='uniform'&&o.r) tip+='  ·  评分 '+o.r+' 星';
    const tail=av.bad&&!on?' · 不可用':(av.unk&&!on?' · 未确认':'');
    // 敌人模板弹窗里的 Uniform 选项：显示星级与预览图
    const rt=(mode!=='uniform'&&o.r)?'<i class="rt" title="评分 '+o.r+' 星">'+('★'.repeat(o.r))+'</i>':'';
    const im=(o.img)?'<span class="pimg"><img src="'+esc(o.img)+'" alt=""></span>':'';
    return '<button class="pill'+(on?' on':'')+(av.bad&&!on?' na':'')+'" data-k="'+esc(key)+
      '" title="'+esc(tip)+'">'+im+
      '<span class="ptxt"><b>'+esc(o.d||o.n)+rt+'</b><s><span class="bc '+bcCls(o.c)+'">×'+(o.c||0)+
      '</span>'+esc(sub)+tail+'</s></span></button>';
  }
  function updateCount(){
    countEl.textContent = mode==='uniform' ? chosen.length : chosenU.size;
    renderSel();
  }
  function renderSel(){
    let items;
    if(mode==='uniform'){
      items=chosen.map(n=>{
        const o=CAT.tpl.find(x=>String(x.n||'').toLowerCase()===n.toLowerCase());
        return {t:n, r:0};
      });
    }else{
      items=[...chosenU].map(i=>{
        const o=CAT.uni.find(x=>x.i===i);
        return {t:o?(o.d||o.n):('#'+i), r:o?o.r:0};
      }).sort((a,b)=>(b.r-a.r)||String(a.t).localeCompare(String(b.t),'zh'));
    }
    selN.textContent=items.length;
    selList.innerHTML=items.length ? items.map(x=>
      '<span class="sel-chip">'+esc(x.t)+(x.r?' <em>'+'★'.repeat(x.r)+'</em>':'')+'</span>'
    ).join('') : '<span class="sel-empty">尚未选择任何内容</span>';
  }
  // 计数直接取 Python 算好的 CAT.pc，与敌人模板表的按钮完全同源
  function paintPreset(){
    const c = (CAT.pc && (CAT.pc[useG] || CAT.pc[0])) || {};
    presetBox.querySelectorAll('button[data-p]').forEach(b=>{
      const pc=b.querySelector('.pc');
      if(pc) pc.textContent=(c[b.dataset.p]!=null?c[b.dataset.p]:0);
    });
    if(presetHint) presetHint.textContent = mode==='uniform'
      ? '按该 Uniform 的性别统计：' + (useG===1?'男':useG===2?'女':'未指定（任一性别可用即算可用）')
      : '';
  }
  function render(){
    paintPreset();
    const q=filter.value.trim().toLowerCase();
    const groups={};
    if(mode==='uniform'){
      CAT.tpl.forEach(o=>{ const g=o.s||'未知来源'; (groups[g]=groups[g]||[]).push(o); });
      const known=new Set(CAT.tpl.map(o=>String(o.n).toLowerCase()));
      const extra=chosen.filter(n=>!known.has(n.toLowerCase()));
      if(extra.length) groups['已选但不在目录中']=extra.map(n=>({n:n,d:n,s:''}));
    }else{
      CAT.uni.forEach(o=>{ const g=ST[o.s]||o.s||'未知'; (groups[g]=groups[g]||[]).push(o); });
    }
    const keys=Object.keys(groups).sort((a,b)=>{
      if(mode!=='uniform'){
        const ia=ST_ORDER.indexOf(a), ib=ST_ORDER.indexOf(b);
        return (ia<0?99:ia)-(ib<0?99:ib);
      }
      if(a==='已选但不在目录中') return -1;
      if(b==='已选但不在目录中') return 1;
      return a.localeCompare(b,'zh');
    });
    const out=[];
    keys.forEach(g=>{
      const list=groups[g].filter(o=>{
        // 已选项永远保留，避免切换预设时“看不见但已选中”
        if(mode==='uniform' && !isOn(o) && !passPreset(o)) return false;
        if(!q) return true;
        return ((o.n||'')+' '+(o.d||'')+' '+g).toLowerCase().includes(q);
      });
      if(!list.length) return;
      list.sort((a,b)=>String(a.d||a.n||'').localeCompare(String(b.d||b.n||''),'zh'));
      out.push('<div class="pgroup"><div class="pg-title">'+esc(g)+'<i>'+list.length+
        '</i></div><div class="pg-list">'+list.map(pill).join('')+'</div></div>');
    });
    groupsBox.innerHTML = out.length ? out.join('')
      : '<div class="pg-empty">没有匹配项。清空筛选试试。</div>';
    updateCount();
  }
  let timer=null;
  filter.addEventListener('input',()=>{ clearTimeout(timer); timer=setTimeout(render,120); });
  function setPreset(p){
    preset=p;
    [...presetBox.querySelectorAll('button')].forEach(x=>x.classList.toggle('on',x.dataset.p===p));
  }
  presetBox.addEventListener('click',e=>{
    const b=e.target.closest('button[data-p]'); if(!b) return;
    setPreset(b.dataset.p); render();
  });
  groupsBox.addEventListener('click',e=>{
    const b=e.target.closest('.pill[data-k]'); if(!b) return;
    if(mode==='uniform'){
      const k=b.dataset.k, low=k.toLowerCase();
      if(chosenLow.has(low)){
        chosenLow.delete(low); chosen=chosen.filter(x=>x.toLowerCase()!==low);
        b.classList.remove('on');
      }else{
        chosenLow.add(low); chosen.push(k); b.classList.add('on');
      }
    }else{
      const k=+b.dataset.k;
      if(chosenU.has(k)){ chosenU.delete(k); b.classList.remove('on'); }
      else{ chosenU.add(k); b.classList.add('on'); }
    }
    updateCount();
  });
  function fitDialog(){
    // 弹窗与页面内容列同宽（body 宽度去掉左右内边距），随「宽度」选项与窗口尺寸变化
    const cs=getComputedStyle(document.body);
    const w=document.body.getBoundingClientRect().width
      - (parseFloat(cs.paddingLeft)||0) - (parseFloat(cs.paddingRight)||0);
    dlg.style.width=Math.max(320, Math.min(w, window.innerWidth*0.94))+'px';
  }
  function start(){
    msg.textContent=''; filter.value=''; render();
    if(!dlg.open) dlg.showModal();
    fitDialog();  // showModal 后滚动条消失，需要此时再量一次
  }
  function openUniform(row){
    mode='uniform'; curIdx=+row.dataset.idx; status=row.dataset.status||''; curTpl='';
    useG=+(row.dataset.gender||0);
    presetBox.style.display='inline-flex';
    setPreset('strict');
    let t=[]; try{ t=JSON.parse(row.dataset.templates||'[]'); }catch(e){}
    chosen=t.slice(); chosenLow=new Set(chosen.map(x=>x.toLowerCase()));
    document.getElementById('dlgTitle').textContent='编辑 Uniform · '+row.querySelector('h2').textContent;
    filter.placeholder='筛选模板名 / 显示名 / 来源 mod…';
    start();
  }
  function openTpl(tpl){
    mode='template'; curTpl=tpl; bindMap=buildBindMap();
    presetBox.style.display='none';  // 这里选的是 Uniform，不涉及敌人模板筛选
    chosenU=new Set(bindMap[tpl.toLowerCase()]||[]);
    document.getElementById('dlgTitle').textContent='编辑敌人模板 · '+tpl;
    filter.placeholder='筛选 Uniform 名 / 状态…';
    start();
  }
  document.getElementById('dlgClose').onclick=()=>dlg.close();
  saveBtn.onclick=async()=>{
    msg.textContent='';
    let allow=false;
    if(mode==='uniform'){
      if(chosen.length && status!=='EUS_NonSoldier'){
        if(!confirm('该 Uniform 当前状态不是「非士兵(敌人)」。\\n保存后会被改为 NonSoldier，它就不再作为士兵 Uniform 使用。\\n继续吗？')) return;
        allow=true;
      }
    }else{
      const bound=bindMap[curTpl.toLowerCase()]||[];
      const need=[...chosenU].filter(i=>!bound.includes(i)).filter(i=>{
        const u=CAT.uni.find(x=>x.i===i);
        return u && u.s!=='EUS_NonSoldier';
      });
      if(need.length){
        if(!confirm('有 '+need.length+' 个 Uniform 当前状态不是「非士兵(敌人)」。\\n保存后它们会被改为 NonSoldier，不再作为士兵 Uniform 使用。\\n继续吗？')) return;
        allow=true;
      }
    }
    saveBtn.disabled=true; saveBtn.textContent='写入中…';
    try{
      const body=mode==='uniform'
        ? {index:curIdx, templates:chosen, allow_status:allow}
        : {template:curTpl, indices:[...chosenU], allow_status:allow};
      const r=await fetch(mode==='uniform'?'/api/bind':'/api/bind_multi',
        {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      const o=await r.json();
      if(!o.ok) throw new Error(o.error||'写入失败');
      dlg.close();
      if(typeof saveNav==='function') saveNav();   // 记住视图与滚动位置，刷新后留在原处
      location.reload();
    }catch(err){
      msg.textContent='写入失败：'+err.message;
      saveBtn.disabled=false; saveBtn.textContent='保存并写入';
    }
  };
  document.querySelectorAll('.row-act button.edit').forEach(b=>{
    b.onclick=()=>openUniform(b.closest('.row'));
  });
  document.querySelectorAll('button.edit-tpl').forEach(b=>{
    b.onclick=()=>openTpl(b.closest('tr').dataset.tpl);
  });
  // Uniform 预览图上传（角色池里没有图片数据，只能由用户上传）
  const imgFile=document.getElementById('imgFile');
  if(imgFile){
    document.querySelectorAll('button.uimg[data-oid]').forEach(b=>{
      b.onclick=()=>{ imgFile.value=''; imgFile.dataset.oid=b.dataset.oid; imgFile.click(); };
    });
    imgFile.onchange=()=>{
      const f=imgFile.files&&imgFile.files[0]; if(!f) return;
      if(f.size>3*1024*1024){ alert('图片超过 3MB，请先压缩'); return; }
      const rd=new FileReader();
      rd.onload=async()=>{
        try{
          const r=await fetch('/api/uniform_image',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({object_id:imgFile.dataset.oid,data:rd.result})});
          const o=await r.json();
          if(!o.ok) throw new Error(o.error||'上传失败');
          const btn=document.querySelector('button.uimg[data-oid="'+imgFile.dataset.oid+'"]');
          if(btn){ btn.classList.add('has'); btn.innerHTML='<img src="'+o.img+'" alt="">'; }
        }catch(err){ alert('上传失败：'+err.message); }
      };
      rd.readAsDataURL(f);
    };
    // × 移除预览图，恢复默认占位
    async function removeImg(oid){
      if(!confirm('移除该 Uniform 的预览图？')) return;
      try{
        const r=await fetch('/api/uniform_image',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({object_id:oid,remove:true})});
        const o=await r.json();
        if(!o.ok) throw new Error(o.error||'移除失败');
        const btn=document.querySelector('button.uimg[data-oid="'+oid+'"]');
        if(btn){ btn.classList.remove('has'); btn.innerHTML='<span>＋图</span>'; }
        const x=document.querySelector('button.uimg-x[data-oid="'+oid+'"]');
        if(x) x.remove();
        const u=(CAT&&CAT.uni)?CAT.uni.find(z=>String(z.oid)===String(oid)):null;
        if(u) u.img='';
      }catch(err){ alert('移除失败：'+err.message); }
    }
    document.querySelectorAll('button.uimg-x[data-oid]').forEach(b=>{
      b.onclick=e=>{ e.stopPropagation(); removeImg(b.dataset.oid); };
    });
  }
  const widthSelEl=document.getElementById('widthSel');
  if(widthSelEl) widthSelEl.addEventListener('change',fitDialog);
  window.addEventListener('resize',fitDialog);
})();
</script>"""


def render_report(pool_path, index, live=False, gender="both"):
    p = Pool(pool_path)
    us = p.uniforms()
    ratings = load_ratings()
    imgs = load_uniform_images()

    def img_url(oid, bust=False):
        fn = imgs.get(str(oid))
        if not fn:
            return ""
        if not bust:
            return "uniform_images/" + fn
        try:
            v = int(os.path.getmtime(os.path.join(UNIFORM_IMAGE_DIR, fn)))
        except Exception:
            v = 0
        return "uniform_images/%s?v=%d" % (fn, v)
    known_tpls = load_known_templates()
    by_tpl = {}
    for u in us:
        for t in u["templates"]:
            by_tpl.setdefault(t.lower(), {"template": t, "uniforms": []})["uniforms"].append(
                {"d": display_name(u), "r": ratings.get(str(u["object_id"]), 0)})

    def chips(templates):
        groups = {}
        for t in templates:
            stem = pretty_template(t)
            groups.setdefault(stem.lower(), {"stem": stem, "exact": [], "templates": []})["exact"].append(t)
            groups[stem.lower()]["templates"].append(t)
        html = []
        for g in sorted(groups.values(), key=lambda x: x["stem"].lower()):
            exact = g["exact"]
            srcs = []
            for t in exact:
                s = source_label(index, t)
                if s and s not in srcs:
                    srcs.append(s)
            title = " | ".join(exact)
            if srcs:
                title += "  //  来源: " + " ; ".join(srcs)
            small = srcs[0] if srcs else "未知来源"
            html.append(
                '<span class="chip" title="%s"><b>%s</b>%s%s</span>' % (
                    esc(title), esc(g["stem"]),
                    '<i>×%d</i>' % len(exact) if len(exact) > 1 else "",
                    '<s>%s</s>' % esc(small)))
        return "\n".join(html)

    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))

    def tri(ent, g):
        """catalog 用的三态：""=可用、"?原因"=未确认、其它=不可用原因"""
        ok, why = availability_reason(ent, g)
        if ok is True:
            return ""
        if ok is None:
            return "?" + (why or "未确认")
        return why or "不可用"

    def avail_cell(ent):
        out = []
        for g in (1, 2):
            if gender == "male" and g != 1:
                continue
            if gender == "female" and g != 2:
                continue
            ok, why = availability_reason(ent, g)
            if ok is True:
                cls, txt = "ok", "可用"
            elif ok is False:
                cls, txt = "bad", "不可用" + ("：" + why if why else "")
            else:
                cls, txt = "unk", "未确认" + ("：" + why if why else "")
            out.append('<span class="%s">%s %s</span>' % (cls, GENDER_NAMES[g], esc(txt)))
        return ' · '.join(out)

    cards = []
    for u in us:
        d = display_name(u)
        rate = ratings.get(str(u["object_id"]), 0)
        search = " ".join([d, u["last"] or "", " ".join(u["templates"])])
        for t in u["templates"]:
            s = source_label(index, t)
            if s:
                search += " " + s
        tpl_attr = (json.dumps(u["templates"], ensure_ascii=False)
                    .replace("&", "&amp;").replace('"', "&quot;")
                    .replace("<", "&lt;").replace(">", "&gt;"))
        if live:
            inner = "".join('<i class="st%s" data-v="%d">%s</i>' % (
                " on" if i <= rate else "", i, "★" if i <= rate else "☆")
                for i in range(1, 6))
            rate_html = ('<span class="rate" data-oid="%s" data-v="%d"'
                         ' title="评级：点星星设置，再点同一颗清除">%s</span>'
                         % (u["object_id"], rate, inner))
            act = '<div class="row-act"><button class="edit">编辑绑定</button></div>'
        else:
            rate_html = ('<span class="rate ro" title="星级">%s</span>' % stars(rate)) if rate else ""
            act = ""
        ui = img_url(u["object_id"], bust=live)
        if live:
            img_html = ('<div class="row-img"><button class="uimg%s" data-oid="%s" '
                        'title="点击上传/替换预览图">%s</button>%s</div>'
                        % (" has" if ui else "", u["object_id"],
                           ('<img src="%s" alt="">' % esc(ui)) if ui else "<span>＋图</span>",
                           ('<button class="uimg-x" data-oid="%s" title="移除预览图，恢复默认">×</button>'
                            % u["object_id"]) if ui else ""))
        else:
            img_html = (('<div class="row-img"><img class="uimg" src="%s" alt=""></div>'
                         % esc(ui)) if ui else '<div class="row-img"></div>')
        cards.append("""
<article class="row" data-idx="%s" data-status="%s" data-gender="%s" data-templates="%s" data-search="%s">
  %s<div class="row-main">
    <div class="row-title">
      <h2>%s</h2>%s
      <span class="tag">%s</span>
      <span class="oid">#%s</span>
      <span class="sub">%s%s</span>
    </div>
    <div class="chips">%s</div>%s
  </div>%s
</article>""" % (
            u["index"], u["status"] or "EUS_NotUniform", u["gender"] or 0,
            tpl_attr, esc(search.lower()), img_html, esc(d), rate_html,
            esc(STATUS_NAMES.get(u["status"], u["status"] or "-")), u["object_id"],
            esc(u["last"] or ""),
            (" · 性别 %s" % ("女" if u["gender"] == 2 else "男" if u["gender"] == 1 else "?")),
            chips(u["templates"]) if u["templates"] else
            '<span class="empty">尚未绑定任何敌人模板</span>',
            ('<p class="warn">状态不是「非士兵(敌人)」，这些绑定在游戏里不会生效</p>'
             if u["templates"] and u["status"] != "EUS_NonSoldier" else ""),
            act))

    rows = []
    p_counts = {p: 0 for p in PRESETS}
    all_keys = set(by_tpl)
    for k, ent in index.items():
        if ent.get("defined"):
            all_keys.add(k)
    for key in sorted(all_keys):
        b = by_tpl.get(key)
        ent = index.get(key) or {}
        t = (b["template"] if b else ent.get("template")) or key
        disp = ent.get("display") or pretty_template(t)
        src = source_label(index, t) or "—"
        act = ('<td><button class="edit-tpl" title="编辑绑定到该模板的 Uniform">编辑</button></td>'
               if live else "")
        if b:
            seen = {}
            for x in b["uniforms"]:
                seen.setdefault(x["d"], x["r"])
            cnt = len(b["uniforms"])
            uni = ('<span class="bc %s" title="被 %d 个 Uniform 绑定">×%d</span>%s' % (
                bind_class(cnt), cnt, cnt,
                "、".join(d + (" " + stars(r) if r else "")
                          for d, r in sorted(seen.items()))))
        else:
            uni = '<span class="bc bc0">×0</span>未绑定'
        strict = 1 if t.lower() in known_tpls else 0
        a3 = avail_any(ent, gender)
        for p in PRESETS:
            if preset_pass(p, known=bool(strict), avail=a3):
                p_counts[p] += 1
        rows.append('<tr data-search="%s" data-tpl="%s" data-bound="%s" data-strict="%s"'
                    ' data-avail="%s"><td class=mono>%s</td>'
                    '<td>%s</td><td>%s</td><td class="avail">%s</td><td>%s</td>%s</tr>' % (
                        esc(t.lower() + " " + disp.lower() + " " + src.lower()),
                        esc(t), "1" if b else "0",
                        str(strict), a3,
                        esc(t), esc(disp), esc(src), avail_cell(ent), uni, act))

    catalog = ""
    if live:
        bound = {}
        for u in us:
            for t in u["templates"]:
                bound.setdefault(t.lower(), t)
        tpls = []
        known = set()
        # 各性别口径下的四档计数，由 Python 统一算出，前端只负责显示
        pc = {m: {p: 0 for p in PRESETS} for m in (0, 1, 2)}

        def count_tpl(ent, k):
            known = k in known_tpls
            for m, g in ((0, "both"), (1, "male"), (2, "female")):
                a3 = avail_any(ent, g)
                for p in PRESETS:
                    if preset_pass(p, known=known, avail=a3):
                        pc[m][p] += 1

        for ent in index.values():
            if not ent.get("defined"):
                continue
            n = ent["template"]
            k = n.lower()
            if k in known:
                continue
            known.add(k)
            tpls.append({"n": n, "d": ent.get("display") or pretty_template(n),
                         "s": source_label(index, n) or "未知来源",
                         "bm": tri(ent, 1), "bf": tri(ent, 2), "bo": tri_any(ent),
                         "g": 1 if k in known_tpls else 0,
                         "c": len(by_tpl.get(k, {}).get("uniforms", []))})
            count_tpl(ent, k)
        for k in sorted(bound):
            if k not in known:
                ent = index.get(k) or {}
                tpls.append({"n": bound[k], "d": pretty_template(bound[k]),
                             "s": "目录外（已安装 mod 的 ini 中没有定义）",
                             "bm": tri(ent, 1), "bf": tri(ent, 2), "bo": tri_any(ent),
                             "g": 1 if k in known_tpls else 0,
                             "c": len(by_tpl.get(k, {}).get("uniforms", []))})
                count_tpl(ent, k)
        unis = [{"i": u["index"], "d": display_name(u), "oid": u["object_id"],
                 "s": u["status"], "r": ratings.get(str(u["object_id"]), 0),
                 "img": img_url(u["object_id"], bust=True),
                 "c": len(u["templates"])} for u in us]
        catalog = json.dumps({
            "tpl": tpls, "uni": unis, "pc": pc,
            # 下拉/排序/配色/预设规则都随数据下发，前端不再手写
            "st": [{"k": k, "d": STATUS_NAMES[k], "o": i}
                   for i, k in enumerate(STATUS_ORDER)],
            "bc": list(BIND_CLASS_LIMITS),
            "pr": PRESET_RULES,
        }, ensure_ascii=False).replace("</", "<\\/")

    n_tpl = len(by_tpl)
    n_total = len(all_keys)
    n_badm = sum(1 for k in all_keys
                 if availability_reason(index.get(k) or {}, 1)[0] is False)
    n_badf = sum(1 for k in all_keys
                 if availability_reason(index.get(k) or {}, 2)[0] is False)
    n_unkm = sum(1 for k in all_keys
                 if availability_reason(index.get(k) or {}, 1)[0] is None)
    n_unkf = sum(1 for k in all_keys
                 if availability_reason(index.get(k) or {}, 2)[0] is None)
    n_bind = sum(len(u["templates"]) for u in us)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>XCOM 2 · Uniform 绑定总览</title>
<style>
:root{--bg:#0e1116;--card:#171b22;--line:#262c36;--txt:#e6e9ef;--dim:#8b94a3;--acc:#7bd88f;--acc2:#5aa9e6;--warn:#e6b355}
*{box-sizing:border-box}
body{margin:0 auto;padding:28px 32px 60px;max-width:100%;background:var(--bg);color:var(--txt);
 font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif}
header{border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:22px}
h1{margin:0 0 6px;font-size:22px;font-weight:600;letter-spacing:.5px}
.meta{color:var(--dim);font-size:12px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 14px;font-size:12px;color:var(--dim)}
.stat b{color:var(--txt);font-size:16px;margin-right:4px}
.toolbar{display:flex;gap:12px;align-items:center;margin:18px 0 8px;flex-wrap:wrap}
input{flex:1;min-width:240px;background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:8px 12px;color:var(--txt);font:inherit;outline:none}
input:focus{border-color:var(--acc2)}
button{background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--dim);
 padding:8px 14px;font:inherit;cursor:pointer}
button.on{color:var(--txt);border-color:var(--acc2);background:#1d2530}
.list{display:flex;flex-direction:column;gap:10px;margin-top:14px}
.row{display:grid;grid-template-columns:auto 1fr auto;gap:16px;align-items:start;
 background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px}
.row:hover{border-color:#3a4452}
.row-img{position:relative;display:flex;align-items:flex-start}
.row-main{min-width:0}
.row-title{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap;margin-bottom:8px}
h2{margin:0;font-size:16px;font-weight:600}
.oid{color:var(--dim);font-size:11px;font-family:Consolas,monospace}
.tag{font-size:11px;color:var(--acc);border:1px solid #2b4433;background:#16241b;border-radius:999px;padding:1px 8px}
.sub{margin:4px 0 10px;color:var(--dim);font-size:12px}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-flex;flex-direction:column;background:#1e242c;border:1px solid var(--line);
 border-radius:7px;padding:4px 9px;font-size:12px;line-height:1.35;max-width:100%}
.chip b{font-weight:600;color:#dfe6f0}
.chip i{font-style:normal;color:var(--acc2);font-size:10px;margin-left:4px}
.chip s{text-decoration:none;color:var(--dim);font-size:10px;margin-top:1px;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap;max-width:230px;display:block}
.empty{color:var(--warn);font-size:12px}
.warn{color:var(--warn);font-size:11px;margin:10px 0 0}
.row-title .sub{margin:0}
.row-act{display:flex;flex-direction:column;gap:8px;align-items:flex-end}
.row-act button{font-size:12px;padding:6px 12px}
.row-act button:hover{color:var(--txt);border-color:var(--acc2)}
.rate{display:inline-flex;gap:1px;font-size:15px;line-height:1;letter-spacing:1px}
.rate .st{font-style:normal;color:#39424f;cursor:pointer;transition:color .12s}
.rate .st:hover{color:#a89468}
.rate .st.on{color:#e6b355}
.rate.ro{font-size:13px;color:#e6b355;letter-spacing:1px}
.row-title .rate{font-size:14px;align-self:center}
.row-title .rate.ro{margin-left:2px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;flex:0 0 auto}
.seg button{border:0;border-radius:0;background:transparent;padding:6px 11px;font-size:12px}
.seg button.on{background:#1d2530;color:var(--txt)}
.seg button:hover{color:var(--txt)}
.seg button i{font-style:normal;font-size:10px;font-weight:600;color:var(--dim);
 margin-left:6px;background:#232b35;border-radius:999px;padding:0 5px;line-height:1.5}
.seg button.on i{color:var(--acc);background:#16241b}
.bc{display:inline-block;font-style:normal;text-decoration:none;font-size:10px;font-weight:600;
 min-width:24px;text-align:center;border-radius:999px;padding:0 5px;margin-right:4px;line-height:1.55}
.bc0{color:#8b94a3;background:#20262e}
.bc1{color:#7bd88f;background:#16241b}
.bc2{color:#5aa9e6;background:#17222e}
.bc3{color:#e6b355;background:#2a2314}
.sel{border:1px solid var(--line);border-radius:8px;background:#1a2027;padding:8px 10px;margin-bottom:12px}
.sel-h{font-size:12px;color:var(--dim);margin-bottom:6px}
.sel-h b{color:var(--txt)}
.sel-list{display:flex;flex-wrap:wrap;gap:5px;max-height:96px;overflow:auto}
.sel-chip{font-size:11px;color:#cfd6e2;background:#232b35;border:1px solid var(--line);
 border-radius:6px;padding:2px 7px;max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sel-chip em{font-style:normal;color:#e6b355;margin-left:3px}
.sel-empty{font-size:11px;color:var(--dim)}
dialog{background:var(--card);color:var(--txt);border:1px solid var(--line);
 border-radius:12px;padding:0;width:min(900px,94vw)}
dialog::backdrop{background:rgba(0,0,0,.62)}
.dlg-head{display:flex;justify-content:space-between;align-items:center;
 padding:14px 18px;border-bottom:1px solid var(--line)}
.dlg-head h3{margin:0;font-size:15px}
.dlg-body{padding:16px 18px;max-height:64vh;overflow:auto}
.dlg-foot{display:flex;justify-content:space-between;align-items:center;gap:12px;
 padding:12px 18px;border-top:1px solid var(--line)}
.dlg-foot .msg{font-size:12px;color:var(--dim)}
.dlg-foot .msg b{color:var(--txt)}
#dlgSave{background:#1f7a4d;border:1px solid #35a86c;color:#eafff3;font-size:15px;font-weight:600;
 padding:10px 26px;border-radius:9px;letter-spacing:.5px;box-shadow:0 2px 12px rgba(38,166,106,.35)}
#dlgSave:hover{background:#26985c;border-color:#4dc487;box-shadow:0 3px 16px rgba(38,166,106,.5)}
#dlgSave:disabled{background:#232a33;border-color:var(--line);color:var(--dim);
 box-shadow:none;font-weight:400}
#dlgFilter{width:100%;margin-bottom:14px}
.pgroups{display:flex;flex-direction:column;gap:14px}
.pgroup{border-top:1px solid var(--line);padding-top:10px}
.pgroup:first-child{border-top:0;padding-top:0}
.pg-title{color:var(--txt);font-size:12px;font-weight:600;margin-bottom:8px;
 display:flex;gap:8px;align-items:baseline;word-break:break-all}
.pg-title i{font-style:normal;color:var(--dim);font-size:11px;font-weight:400}
.pg-list{display:flex;flex-wrap:wrap;gap:6px}
.pill{display:flex;flex-direction:row;align-items:center;gap:8px;text-align:left;
 background:#1e242c;border:1px solid var(--line);border-radius:7px;padding:5px 10px;
 color:var(--dim);line-height:1.3;max-width:250px}
.ptxt{display:flex;flex-direction:column;align-items:flex-start;gap:1px;min-width:0}
.pill b{font-size:12px;font-weight:600;color:#dfe6f0;max-width:210px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill .rt{font-style:normal;color:#e6b355;font-size:10px;letter-spacing:1px;margin-left:5px}
.pimg{flex:0 0 auto;align-self:center;width:28px;height:56px;border-radius:6px;border:1px solid var(--line);
 background:#10141a;overflow:hidden;display:flex;align-items:center;justify-content:center}
.pimg img{width:100%;height:100%;object-fit:cover;display:block}
.uimg{flex:0 0 auto;width:56px;height:112px;padding:0;border-radius:8px;
 border:1px solid var(--line);background:#10141a;overflow:hidden;display:flex;
 align-items:center;justify-content:center;color:var(--dim);font-size:11px;cursor:pointer}
.uimg img{width:100%;height:100%;object-fit:cover;display:block}
.uimg:hover{border-color:var(--acc2);color:var(--txt)}
.uimg-x{position:absolute;top:-7px;right:-7px;width:20px;height:20px;padding:0;border-radius:50%;
 border:1px solid var(--line);background:#1a2029;color:var(--dim);font-size:13px;line-height:1;
 display:flex;align-items:center;justify-content:center;z-index:2}
.uimg-x:hover{color:#ff8a80;border-color:#5a2b2b;background:#241a1a}
img.uimg{cursor:default}
.pill s{text-decoration:none;font-size:10px;color:var(--dim);max-width:210px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pill:hover{border-color:var(--acc2);color:var(--txt)}
.pill.on{background:#16241b;border-color:#2b4433;color:var(--acc)}
.pill.on b{color:var(--acc)}
.pill.on s{color:#5f8a6c}
.pill.na{opacity:.6}
.pill.na b{color:#b7bec9}
#dlgPreset{margin:0 0 14px;display:inline-flex}
#dlgPresetHint{vertical-align:middle}
.pg-empty{color:var(--dim);font-size:12px}
.edit-tpl{font-size:12px;padding:4px 10px}
.edit-tpl:hover{color:var(--txt);border-color:var(--acc2)}
table{width:100%;border-collapse:collapse;margin-top:14px;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:500;position:sticky;top:0;background:var(--bg);font-size:12px}
.mono{font-family:Consolas,monospace;font-size:12px;color:#cfd6e2}
.na{display:block;font-size:11px;color:#b0764a;margin-top:2px}
.avail .ok{color:#7bd88f}
.avail .bad{color:#c27a5a}
.avail .unk{color:var(--warn)}
.chk{display:flex;align-items:center;gap:7px;color:var(--dim);font-size:12px;cursor:pointer;flex:0 0 auto}
.chk input{flex:0 0 auto;width:15px;height:15px;min-width:0;padding:0;margin:0;accent-color:#7bd88f}
.v2bar{margin-top:12px;justify-content:flex-start}
.hint{color:var(--dim);font-size:12px}
.wsel{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px;flex:0 0 auto}
.wsel select{background:var(--card);border:1px solid var(--line);border-radius:8px;color:var(--txt);
 padding:7px 8px;font:inherit;font-size:12px;outline:none;cursor:pointer}
.wsel select:focus{border-color:var(--acc2)}
.hidden{display:none}
</style></head><body>
<header>
  <h1>XCOM 2 · Uniform Management 绑定总览</h1>
  <div class="meta">数据: {{POOL}} &nbsp;·&nbsp; 生成于 {{NOW}} &nbsp;·&nbsp; 读取自角色池二进制 ExtraDatas &nbsp;·&nbsp; {{MODE}}</div>
  <div class="stats">
    <div class="stat"><b>{{NUNIFORM}}</b>个 Uniform</div>
    <div class="stat"><b>{{NTPL}}</b>个已绑定模板</div>
    <div class="stat"><b>{{NTOTAL}}</b>个模板（全部）</div>
    <div class="stat"><b>{{NBADM}}</b>个男单位不可用 · <b>{{NUNKM}}</b>个未确认</div>
    <div class="stat"><b>{{NBADF}}</b>个女单位不可用 · <b>{{NUNKF}}</b>个未确认</div>
    <div class="stat"><b>{{NBIND}}</b>条绑定</div>
    <div class="stat"><b>{{NMOD}}</b>个已安装 mod 索引</div>
  </div>
</header>
<div class="toolbar">
  <input id="q" placeholder="搜索 Uniform 名 / 模板名 / 来源 mod ...">
  <button id="b1" class="on">按 Uniform</button>
  <button id="b2">按 敌人模板</button>
  <button id="reload" title="{{RELOAD_TIP}}">⟳ 更新数据</button>{{EXPORT_BTN}}
  <span class="wsel" title="内容占浏览器宽度的比例，两侧留空；选择会记住">
    <span>宽度</span>
    <select id="widthSel">
      <option value="100">100%（铺满）</option>
      <option value="90">90%</option>
      <option value="80">80%</option>
      <option value="70">70%</option>
      <option value="60">60%</option>
    </select>
  </span>
</div>
<input type="file" id="imgFile" accept="image/png,image/jpeg,image/webp,image/gif" style="display:none">
<section id="v1"><div class="list" id="list">{{CARDS}}</div></section>
<section id="v2" class="hidden">
  <div class="toolbar v2bar">
    <div class="seg" id="tplPreset" title="按 {{GENDER_LABEL}} 口径统计">
      <button data-p="strict" class="on">按AM规则（严格）<i>{{P_STRICT}}</i></button>
      <button data-p="approx">按AM规则（近似）<i>{{P_APPROX}}</i></button>
      <button data-p="usable">全部可用模板<i>{{P_USABLE}}</i></button>
      <button data-p="all">全部模板<i>{{P_ALL}}</i></button>
    </div>
    <span class="hint">已绑定 {{NTPL}} 个 · 全部 {{NTOTAL}} 个 · 男单位不可用 {{NBADM}} 个 / 女单位不可用 {{NBADF}} 个（按 AM 规则）</span>
  </div>
  <table><thead><tr><th style="width:22%">模板 (DataName)</th><th style="width:16%">显示名</th>
  <th style="width:18%">来源 mod</th><th style="width:24%">可用性（{{GENDER_LABEL}}）</th>
  <th>绑定的 Uniform</th>{{TH_ACT}}</tr></thead>
  <tbody id="tbody">{{ROWS}}</tbody></table>
</section>
<script>
const PR={{PRESET_JSON}};
const q=document.getElementById('q');
const tplPresetEl=document.getElementById('tplPreset');
const list=[...document.querySelectorAll('#list .row')];
const rows=[...document.querySelectorAll('#tbody tr')];
let tplPreset='strict';
// 四档预设：与 CLI / 弹窗共用 Python 的 PRESET_RULES
function passPreset(r){
 const rule=PR[tplPreset]||{};
 if(rule.need_known&&r.dataset.strict!=='1') return false;
 if(!rule.avail) return true;
 return rule.avail.indexOf(r.dataset.avail)>=0;
}
function applyFilter(){
 const v=q.value.trim().toLowerCase();
 list.forEach(c=>c.classList.toggle('hidden',!c.dataset.search.includes(v)));
 rows.forEach(r=>{
   const okQ=!v||r.dataset.search.includes(v);
   r.classList.toggle('hidden',!(okQ&&passPreset(r)));
 });
}
q.addEventListener('input',applyFilter);
tplPresetEl.addEventListener('click',e=>{
 const b=e.target.closest('button[data-p]'); if(!b) return;
 tplPreset=b.dataset.p;
 [...tplPresetEl.querySelectorAll('button')].forEach(x=>x.classList.toggle('on',x===b));
 applyFilter();
});
applyFilter();
function setView(v){
 const is1=(v!=='v2');
 document.getElementById('b1').classList.toggle('on',is1);
 document.getElementById('b2').classList.toggle('on',!is1);
 document.getElementById('v1').classList.toggle('hidden',!is1);
 document.getElementById('v2').classList.toggle('hidden',is1);
 applyFilter();
}
// 记下当前视图与滚动位置：刷新（保存写入、重新读取）后回到原处，而不是跳回「按 Uniform」
function saveNav(){
 try{
  localStorage.setItem('ut_view',document.getElementById('v2').classList.contains('hidden')?'v1':'v2');
  localStorage.setItem('ut_scroll',String(window.scrollY||0));
 }catch(e){}
}
document.getElementById('b1').onclick=()=>{setView('v1');saveNav();};
document.getElementById('b2').onclick=()=>{setView('v2');saveNav();};
document.getElementById('reload').onclick=()=>{saveNav();location.reload();};
(function(){
 let v='v1';
 try{ v=localStorage.getItem('ut_view')||'v1'; }catch(e){}
 if(v==='v2') setView('v2');
 let y=0;
 try{ y=+(localStorage.getItem('ut_scroll')||0); localStorage.removeItem('ut_scroll'); }catch(e){}
 if(y>0) requestAnimationFrame(()=>window.scrollTo(0,y));
})();
function paintRate(box,v){
 box.dataset.v=v;
 box.querySelectorAll('.st').forEach((st,i)=>{
   const on=i<v;
   st.classList.toggle('on',on);
   st.textContent=on?'★':'☆';
 });
}
document.querySelectorAll('.rate[data-oid]').forEach(box=>{
 box.querySelectorAll('.st').forEach(st=>{
   st.onclick=async()=>{
     const v=+st.dataset.v, cur=+box.dataset.v, nv=(cur===v?0:v);
     paintRate(box,nv);
     try{
       const r=await fetch('/api/rating',{method:'POST',
         headers:{'Content-Type':'application/json'},
         body:JSON.stringify({object_id:box.dataset.oid,rating:nv})});
       const o=await r.json();
       if(!o.ok) throw new Error(o.error||'保存失败');
       paintRate(box,o.rating);
     }catch(e){ paintRate(box,cur); alert('评级保存失败：'+e.message); }
   };
 });
});
(function(){  // 内容宽度比例：居中显示、两侧留空，选择写 localStorage
 const sel=document.getElementById('widthSel');
 if(!sel) return;
 const apply=p=>{ document.body.style.maxWidth=p+'%'; };
 let saved='80';
 try{ saved=localStorage.getItem('ut_width')||'80'; }catch(e){}
 if(![...sel.options].some(o=>o.value===saved)) saved='80';
 sel.value=saved; apply(saved);
 sel.addEventListener('change',()=>{
   apply(sel.value);
   try{ localStorage.setItem('ut_width',sel.value); }catch(e){}
 });
})();
(function(){  // 导出计划：JSON + 游戏配置 INI，供 UniformToolApply mod 应用
 const btn=document.getElementById('exportBtn');
 if(!btn) return;
 btn.onclick=async()=>{
   btn.disabled=true; const t=btn.textContent; btn.textContent='导出中…';
   try{
     const r=await fetch('/api/export_plan',{method:'POST'});
     const o=await r.json();
     if(!o.ok) throw new Error(o.error||'导出失败');
     alert('已导出 '+o.count+' 个 Uniform（其中 '+o.with_templates+' 个带模板绑定）：\\n\\n'
       +'JSON：'+o.json+'\\nINI ：'+o.ini+'\\n\\n'
       +'请先导出再启动游戏；进游戏打开角色池管理器，UniformToolApply mod 会弹窗询问是否应用。');
   }catch(err){ alert('导出失败：'+err.message); }
   btn.disabled=false; btn.textContent=t;
 };
})();
</script>
{{EDIT_BLOCK}}
</body></html>"""
    html = (html
            .replace("{{POOL}}", esc(pool_path))
            .replace("{{NOW}}", now)
            .replace("{{NUNIFORM}}", str(len(us)))
            .replace("{{NTPL}}", str(n_tpl))
            .replace("{{NTOTAL}}", str(n_total))
            .replace("{{NBADM}}", str(n_badm))
            .replace("{{NBADF}}", str(n_badf))
            .replace("{{NUNKM}}", str(n_unkm))
            .replace("{{NUNKF}}", str(n_unkf))
            .replace("{{P_STRICT}}", str(p_counts["strict"]))
            .replace("{{P_APPROX}}", str(p_counts["approx"]))
            .replace("{{P_USABLE}}", str(p_counts["usable"]))
            .replace("{{P_ALL}}", str(p_counts["all"]))
            .replace("{{GENDER_LABEL}}", {"male": "男", "female": "女"}.get(gender, "男 / 女"))
            .replace("{{NBIND}}", str(n_bind))
            .replace("{{NMOD}}", str(len(index)))
            .replace("{{CARDS}}", "\n".join(cards))
            .replace("{{ROWS}}", "\n".join(rows))
            .replace("{{TH_ACT}}", '<th style="width:8%">操作</th>' if live else "")
            .replace("{{EDIT_BLOCK}}", EDITOR_HTML if live else "")
            .replace("{{CATALOG}}", catalog)
            .replace("{{PRESET_JSON}}", json.dumps(PRESET_RULES))
            .replace("{{MODE}}", "实时模式 · 每次刷新页面都会重新读取角色池" if live
                     else "静态快照 · 数据不会自动变化")
            .replace("{{RELOAD_TIP}}", "重新读取 DefaultCharacterPool.bin" if live
                     else "静态文件只能重载本页；要拿到最新数据请运行 report 或 serve")
            .replace("{{EXPORT_BTN}}",
                     '<button id="exportBtn" title="把当前绑定计划导出为 JSON + 游戏配置（XComUniformTool.ini），'
                     '供 UniformToolApply mod 在游戏内应用">⇩ 导出计划</button>' if live else ""))
    return html


def build_report(pool_path, out_path, index, gender="both"):
    html = render_report(pool_path, index, live=False, gender=gender)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


# ----------------------------------------------------------------------------
# 命令：serve（本地实时预览，每次刷新都重新读取角色池）
# ----------------------------------------------------------------------------


def serve(pool_path, index, port=8765, open_browser=True, gender="both",
          workshop_dir=None, cache_path=None):
    import threading
    import time
    import traceback
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock = threading.Lock()

    # 索引热更新：打开页面时比一次 mod 目录指纹，变了就在后台重建（重建约 9 秒，
    # 不能放进请求线程），本次先用旧索引渲染并提示稍后刷新。
    idx = {"data": index, "src": _source_state(workshop_dir),
           "building": False, "last_try": 0.0}
    toast = ('<div style="position:fixed;left:50%;bottom:24px;'
             'transform:translateX(-50%);z-index:9999;background:#2a1f10;'
             'color:#e6b355;border:1px solid #6b4f1d;padding:10px 16px;'
             'border-radius:8px;font:13px system-ui;box-shadow:0 6px 20px #0008">'
             '检测到 mod 目录有变动，正在后台重建模板索引，约 10 秒后刷新本页</div>')

    def ensure_index():
        """目录指纹变化时启动一次后台重建，返回 True 表示正在重建。"""
        if not workshop_dir or idx["building"]:
            return idx["building"]
        cur = _source_state(workshop_dir)
        if cur == idx["src"] or time.time() - idx["last_try"] < 30:
            return False
        idx["building"] = True
        idx["last_try"] = time.time()

        def work():
            try:
                idx["data"] = build_mod_index(workshop_dir, cache_path, verbose=True)
                idx["src"] = _source_state(workshop_dir)
                print("模板索引已重建：%d 条（刷新页面生效）" % len(idx["data"]))
            except Exception as e:
                print("模板索引重建失败: %s: %s" % (type(e).__name__, e),
                      file=sys.stderr)
            finally:
                idx["building"] = False

        threading.Thread(target=work, daemon=True).start()
        return True

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = self.path.partition("?")[0]
            if path.startswith("/uniform_images/"):
                fn = os.path.basename(path[len("/uniform_images/"):])
                m = IMAGE_NAME_RE.match(fn)
                fp = os.path.join(UNIFORM_IMAGE_DIR, fn)
                if not m or not os.path.isfile(fp):
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                ctype = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                         "webp": "image/webp", "gif": "image/gif"}[m.group(2).lower()]
                with open(fp, "rb") as f:
                    body = f.read()
                self._send(200, body, ctype)
                return
            if path not in ("/", "/index.html"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            try:
                rebuilding = ensure_index()
                html = render_report(pool_path, idx["data"], live=True, gender=gender)
                if rebuilding:
                    html = html.replace("</body>", toast + "</body>", 1)
                self._send(200, html.encode("utf-8"))
            except Exception:
                err = traceback.format_exc().replace("<", "&lt;")
                page = ("<!DOCTYPE html><meta charset='utf-8'>"
                        "<body style='background:#0e1116;color:#e6b355;"
                        "font:13px Consolas,monospace;padding:24px'>"
                        "<h2 style='color:#e6e9ef;font-family:inherit'>读取角色池失败</h2>"
                        "<pre style='white-space:pre-wrap'>%s</pre>"
                        "<p style='color:#8b94a3'>确认游戏已关闭、文件路径正确后刷新本页。</p>"
                        "</body>" % err)
                self._send(500, page.encode("utf-8"))

        def do_POST(self):
            path = self.path.split("?")[0]
            if path == "/api/export_plan":
                try:
                    with lock:
                        r = export_plan(pool_path)
                    self._json(200, {"ok": True, **r})
                except Exception as e:
                    self._json(400, {"ok": False, "error": str(e)})
                return
            if path not in ("/api/bind", "/api/bind_multi", "/api/rating",
                            "/api/uniform_image"):
                self._json(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                allow = bool(payload.get("allow_status"))
                if path == "/api/rating":
                    oid = str(payload.get("object_id") or "").strip()
                    if not oid.isdigit():
                        raise ValueError("object_id 非法")
                    try:
                        v = int(payload.get("rating") or 0)
                    except Exception:
                        raise ValueError("rating 必须是 0-5")
                    if not 0 <= v <= 5:
                        raise ValueError("rating 必须在 0-5 之间")
                    with lock:
                        data = load_ratings()
                        if v:
                            data[oid] = v
                        else:
                            data.pop(oid, None)
                        save_ratings(data)
                    self._json(200, {"ok": True, "object_id": oid, "rating": v})
                    return
                if path == "/api/uniform_image":
                    oid = str(payload.get("object_id") or "").strip()
                    if not oid.isdigit():
                        raise ValueError("object_id 非法")
                    if payload.get("remove"):
                        with lock:
                            remove_uniform_images(oid)
                        self._json(200, {"ok": True, "img": ""})
                        return
                    data = payload.get("data") or ""
                    m = re.match(r"^data:image/(png|jpeg|jpg|webp|gif);base64,",
                                 data, re.I)
                    if not m:
                        raise ValueError("只支持 PNG / JPG / WEBP / GIF")
                    ext = m.group(1).lower()
                    if ext == "jpeg":
                        ext = "jpg"
                    raw = base64.b64decode(data.split(",", 1)[1])
                    if len(raw) > 3 * 1024 * 1024:
                        raise ValueError("图片超过 3MB，请先压缩")
                    os.makedirs(UNIFORM_IMAGE_DIR, exist_ok=True)
                    with lock:
                        remove_uniform_images(oid)
                        fn = "%s.%s" % (oid, ext)
                        tmp = os.path.join(UNIFORM_IMAGE_DIR, fn + ".tmp")
                        with open(tmp, "wb") as f:
                            f.write(raw)
                        os.replace(tmp, os.path.join(UNIFORM_IMAGE_DIR, fn))
                        v = int(os.path.getmtime(os.path.join(UNIFORM_IMAGE_DIR, fn)))
                    self._json(200, {"ok": True,
                                     "img": "uniform_images/%s?v=%d" % (fn, v)})
                    return
                if path == "/api/bind":
                    idx = int(payload.get("index"))
                    tpls = payload.get("templates") or []
                    if not isinstance(tpls, list):
                        raise ValueError("templates 必须是数组")
                    with lock:
                        res = set_element_templates(pool_path, idx, tpls,
                                                    allow_status_change=allow)
                    print("写入 #%d: %s" % (idx, ", ".join(res["added"]) or "(无新增)"))
                else:
                    tpl = str(payload.get("template") or "")
                    idxs = payload.get("indices") or []
                    if not isinstance(idxs, list):
                        raise ValueError("indices 必须是数组")
                    with lock:
                        res = set_uniforms_for_template(pool_path, tpl, idxs,
                                                        allow_status_change=allow)
                    print("模板 %s: 更新 %d 个 Uniform" % (tpl, len(res["updated"])))
                self._json(200, res)
            except SystemExit as e:
                self._json(409, {"ok": False, "error": str(e)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False,
                                 "error": "%s: %s" % (type(e).__name__, e)})

        def log_message(self, fmt, *args):
            print("[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), fmt % args))

    url = "http://127.0.0.1:%d/" % port
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit("端口 %d 被占用（%s）。换一个: --port 8766" % (port, e))
    print("本地预览已启动: %s" % url)
    print("每次刷新页面都会重新读取角色池；mod 目录有变动会在后台自动重建索引。")
    print("Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()


# ----------------------------------------------------------------------------
# 命令：bind / unbind / status
# ----------------------------------------------------------------------------


def find_uniform(pool, spec):
    us = pool.uniforms()
    spec_l = spec.strip().lower()
    if spec_l.startswith("#") and spec_l[1:].isdigit():
        oid = int(spec_l[1:])
        hits = [u for u in us if u["object_id"] == oid]
        if hits:
            return hits[0]
        raise ValueError("没有 ObjectID 为 %d 的 Uniform" % oid)
    exact = [u for u in us
             if spec_l in (display_name(u).lower(), (u["last"] or "").lower())]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ValueError("有 %d 个 Uniform 都叫 %r，请改用 #ObjectID 指定：\n%s" % (
            len(exact), spec,
            "\n".join("  #%s  %s (%s)" % (u["object_id"], display_name(u),
                                         u["last"] or "") for u in exact)))
    hits = [u for u in us if spec_l in display_name(u).lower()
            or spec_l in (u["last"] or "").lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError("找不到唯一的 Uniform: %r%s" % (
        spec, ("（候选: %s）" % "、".join("#%s %s" % (h["object_id"], display_name(h))
                                       for h in hits)) if hits else ""))


def element_name(d):
    cp = d.get("CharPoolData") or {}
    nick = (cp.get("strNickName") or "").strip().strip("'").strip('"').strip()
    if nick:
        return nick
    return (cp.get("strLastName") or "").strip() or ("#%s" % d.get("ObjectID"))


# >>> WRITE-CORE B
def _plan_templates(pool, idx, new, status=None):
    """算出某条目的改动字节，只描述「要改什么」，不做允许性判定。

    返回 edits / old / old_status / new_status / templates / zero_allowed /
    needs_status_change（新模板非空、且状态需要从非 NonSoldier 自动改成 NonSoldier）。
    """
    d = pool.element_dict(pool.elems[idx])
    old = list(d.get("NonSoldierUniformTemplates") or [])
    cur_status = d.get("UniformStatus") or "EUS_NotUniform"

    needs = bool(new) and not status and cur_status != "EUS_NonSoldier"
    new_status = status or ("EUS_NonSoldier" if needs else cur_status)

    edits = {}
    if new != old or d.get("NonSoldierUniformTemplates") is None:
        edits["NonSoldierUniformTemplates"] = build_name_array_tag(
            "NonSoldierUniformTemplates", new)
    if new_status != cur_status:
        edits["UniformStatus"] = build_byte_tag("UniformStatus", "EUniformStatus", new_status)
    return {
        "edits": edits, "old": old, "old_status": cur_status,
        "new_status": new_status, "templates": new,
        "needs_status_change": needs,
        # AM 存盘时会把 Uniform 的 bAllowedType* 全部置 false，保持一致
        "zero_allowed": new_status != "EUS_NotUniform",
    }


def _commit(pool, plans):
    """把一批已规划的改动一次写盘（apply → verify → write），返回备份路径或 None。

    只处理 edits 非空的条目；no_allowed 由 zero_allowed 统一推导。
    """
    todo = [x for x in plans if x["edits"]]
    if not todo:
        return None
    element_edits, expected, zero = {}, {}, []
    for plan in todo:
        idx = plan["idx"]
        element_edits[idx] = plan["edits"]
        expected[idx] = {"templates": plan["templates"],
                         "status": plan["new_status"]}
        if plan["zero_allowed"]:
            zero.append(idx)
            expected[idx]["no_allowed"] = True
    new_data = apply_edits(pool, element_edits, zero_allowed=tuple(zero))
    verify(pool, new_data, expected)
    return write_pool(pool, new_data, expected)


def _dedupe(names):
    out, seen = [], set()
    for t in names:
        t = str(t).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def set_element_templates(pool_path, idx, templates, allow_status_change=False, status=None):
    """按 ExtraDatas 下标直接设置某 Uniform 的敌人模板列表。返回变更结果。"""
    p = Pool(pool_path)
    if not (0 <= idx < p.count):
        raise ValueError("下标越界: %d（共 %d 条）" % (idx, p.count))

    new = _dedupe(templates)
    plan = _plan_templates(p, idx, new, status=status)
    if plan["needs_status_change"] and not allow_status_change:
        cur = plan["old_status"]
        raise ValueError("该 Uniform 当前状态是 %s，改成敌人绑定需要同时把状态设为 NonSoldier"
                         % STATUS_NAMES.get(cur, cur))

    old = plan["old"]
    old_set = {x.lower() for x in old}
    new_set = {x.lower() for x in new}
    result = {
        "ok": True, "index": idx,
        "old": old, "new": new,
        "old_status": plan["old_status"], "new_status": plan["new_status"],
        "added": [x for x in new if x.lower() not in old_set],
        "removed": [x for x in old if x.lower() not in new_set],
        "changed": False, "backup": None,
    }
    plan["idx"] = idx
    bak = _commit(p, [plan])
    if bak:
        result["backup"] = bak
        result["changed"] = True
    return result


def set_uniforms_for_template(pool_path, template, indices, allow_status_change=False):
    """把某个敌人模板挂到给定的一批 Uniform 上，或从这些 Uniform 上摘掉（一次写盘）。"""
    p = Pool(pool_path)
    template = str(template or "").strip()
    if not template:
        raise ValueError("模板名为空")
    idxs = sorted({int(i) for i in indices})

    plans, blocked = [], []
    for idx in idxs:
        if not (0 <= idx < p.count):
            raise ValueError("下标越界: %d（共 %d 条）" % (idx, p.count))
        d = p.element_dict(p.elems[idx])
        old = list(d.get("NonSoldierUniformTemplates") or [])
        has = template.lower() in {x.lower() for x in old}
        new = [x for x in old if x.lower() != template.lower()] if has else old + [template]
        plan = _plan_templates(p, idx, new)
        plan["idx"] = idx
        plan["name"] = element_name(d)
        if plan["needs_status_change"]:
            blocked.append(plan["name"])
        plans.append(plan)

    if blocked and not allow_status_change:
        raise ValueError("%d 个 Uniform 需要改成 NonSoldier 才能生效（%s%s）"
                         % (len(blocked), "、".join(blocked[:5]),
                            " 等" if len(blocked) > 5 else ""))

    result = {
        "ok": True, "template": template, "changed": False, "backup": None,
        "updated": [], "status_changed": [],
    }
    for plan in plans:
        old_set = {x.lower() for x in plan["old"]}
        new_set = {x.lower() for x in plan["templates"]}
        if plan["edits"]:
            result["updated"].append({
                "index": plan["idx"], "name": plan["name"],
                "added": [x for x in plan["templates"] if x.lower() not in old_set],
                "removed": [x for x in plan["old"] if x.lower() not in new_set],
            })
        if plan["new_status"] != plan["old_status"]:
            result["status_changed"].append({
                "index": plan["idx"], "name": plan["name"],
                "old": plan["old_status"], "new": plan["new_status"],
            })

    bak = _commit(p, plans)
    if bak:
        result["backup"] = bak
        result["changed"] = True
    return result
# <<< WRITE-CORE B


def do_bind(pool_path, spec, templates, remove=False, status=None, set_status=False):
    p = Pool(pool_path)
    u = find_uniform(p, spec)
    d = p.element_dict(p.elems[u["index"]])
    old = list(d.get("NonSoldierUniformTemplates") or [])

    if remove:
        drop = {t.lower() for t in templates}
        new = [x for x in old if x.lower() not in drop]
    else:
        # 去重由 set_element_templates 统一负责
        new = old + list(templates)

    try:
        res = set_element_templates(pool_path, u["index"], new,
                                    allow_status_change=set_status, status=status)
    except ValueError as e:
        raise ValueError(str(e) + "\n（如果确认要把该 Uniform 改成非士兵，加 --set-status）")

    print("Uniform: %s  (#%s)" % (display_name(u), u["object_id"]))
    print("  状态: %s -> %s" % (STATUS_NAMES.get(res["old_status"], res["old_status"]),
                              STATUS_NAMES.get(res["new_status"], res["new_status"])))
    print("  模板: %d -> %d" % (len(res["old"]), len(res["new"])))
    for t in res["added"]:
        print("    + %s" % t)
    for t in res["removed"]:
        print("    - %s" % t)
    if not res["changed"]:
        print("没有变化。")
        return
    print("\n已写入: %s" % pool_path)
    print("备份:   %s" % res["backup"])


# ----------------------------------------------------------------------------
# 命令：corehash / selftest（写盘核心完整性）
# ----------------------------------------------------------------------------


def cmd_corehash(update=False):
    cur = core_digests()
    print("A: %s" % cur["A"])
    print("B: %s" % cur["B"])
    try:
        with open(CORE_LOCK_FILE, encoding="utf-8") as f:
            lock = json.load(f)
    except Exception:
        lock = {}
    if not update:
        if lock.get("A") == cur["A"] and lock.get("B") == cur["B"]:
            print("与锁文件一致。")
        else:
            print("与锁文件不一致。如属误改请恢复备份；确属有意修改，复核后运行 corehash --update。")
        return
    body = {
        "A": cur["A"], "B": cur["B"],
        "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "note": "写盘核心两段代码的 SHA-256；修改前必须人工复核二进制补丁逻辑",
    }
    tmp = CORE_LOCK_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CORE_LOCK_FILE)
    print("已更新: %s" % CORE_LOCK_FILE)


def cmd_selftest(pool_path):
    """写盘核心自检：只在内存里做一次完整补丁 + 校验，不落盘、不改真实角色池。"""
    check_write_core()
    p = Pool(pool_path)
    if apply_edits(p, {}, zero_allowed=()) != p.data:
        raise SystemExit("自检失败：空编辑模式改动了字节")
    picked = None
    for el in p.elems:
        if p.element_dict(el).get("CharPoolData") is not None:
            picked = el["index"]
            break
    if picked is None:
        raise SystemExit("自检失败：找不到含 CharPoolData 的条目")
    d = p.element_dict(p.elems[picked])
    old_t = list(d.get("NonSoldierUniformTemplates") or [])
    added = "ZZ_UniformToolSelftest"
    new_t = old_t if added.lower() in {x.lower() for x in old_t} else old_t + [added]
    plan = _plan_templates(p, picked, new_t, status="EUS_NonSoldier")
    cur_status, new_status = plan["old_status"], plan["new_status"]
    expected = {picked: {"templates": new_t, "status": new_status, "no_allowed": True}}
    new_data = apply_edits(p, {picked: plan["edits"]}, zero_allowed=(picked,))
    p2 = verify(p, new_data, expected)
    got = p2.element_dict(p2.elems[picked]).get("NonSoldierUniformTemplates") or []
    if added.lower() not in {x.lower() for x in got}:
        raise SystemExit("自检失败：往返后模板未写入")
    print("自检通过：条目 #%d，字节增减 %+d，状态 %s -> %s，其余条目与文件头尾逐字节未变"
          % (picked, len(new_data) - len(p.data), cur_status, new_status))


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------


def main():
    """CLI 入口：用户输入错误统一是 ValueError，这里转成退出信息。"""
    try:
        _run()
    except ValueError as e:
        raise SystemExit(str(e))


def _run():
    ap = argparse.ArgumentParser(
        description="XCOM2 Uniform Management 绑定离线工具（读写 DefaultCharacterPool.bin）")
    ap.add_argument("cmd", choices=["list", "report", "serve", "bind", "unbind", "status",
                                    "templates", "corehash", "selftest", "export"])
    ap.add_argument("--pool", default=None, help="角色池路径，默认 Documents\\My Games\\XCOM2 War of the Chosen\\...")
    ap.add_argument("--uniform", help="Uniform 名（中文昵称或英文名，或 #ObjectID）")
    ap.add_argument("--template", action="append", default=[], help="敌人角色模板 DataName，可重复")
    ap.add_argument("--value", help="status 命令的新值")
    ap.add_argument("--set-status", action="store_true", help="允许把非 NonSoldier 的 Uniform 改成 NonSoldier")
    ap.add_argument("--out", default=None, help="report 输出路径")
    ap.add_argument("--mods", default=None, help="workshop\\content\\268500 目录")
    ap.add_argument("--refresh", action="store_true", help="重新扫描 mod（忽略缓存）")
    ap.add_argument("--search", default=None, help="templates 命令的搜索词")
    ap.add_argument("--preset", choices=list(PRESETS), default=None,
                    help="templates 命令的筛选预设，与页面四处按钮同一口径")
    ap.add_argument("--port", type=int, default=8765, help="serve 端口，默认 8765")
    ap.add_argument("--no-open", action="store_true", help="serve 时不自动打开浏览器")
    ap.add_argument("--gender", choices=["both", "male", "female"], default="both",
                    help="report/serve 里按哪个性别计算并展示可用性，默认 both（男女各算一遍）")
    ap.add_argument("--update", action="store_true", help="corehash 命令：把当前哈希写入锁文件")
    args = ap.parse_args()

    if args.cmd == "corehash":
        cmd_corehash(update=args.update)
        return

    pool_path = args.pool or default_pool_path()
    if not os.path.isfile(pool_path):
        raise SystemExit("找不到角色池文件: %s" % pool_path)

    if args.cmd == "selftest":
        cmd_selftest(pool_path)
        return

    if args.cmd == "export":
        r = export_plan(pool_path)
        print("已导出 %d 个 Uniform（其中 %d 个带模板绑定）" % (r["count"], r["with_templates"]))
        print("JSON: " + r["json"])
        print("INI : " + r["ini"])
        print("请先导出再启动游戏；进游戏打开角色池管理器，UniformToolApply mod 会弹窗询问是否应用。")
        return

    here = os.path.dirname(os.path.abspath(__file__))
    cache = os.path.join(here, "mod_index_cache.json")

    if args.cmd == "list":
        cmd_list(pool_path)
        return

    workshop = args.mods
    if not workshop:
        dirs = guess_workshop_dirs()
        workshop = dirs[0] if dirs else None

    index = {}
    if args.cmd in ("report", "serve", "templates"):
        index = build_mod_index(workshop, cache, refresh=args.refresh, verbose=True)

    if args.cmd == "serve":
        serve(pool_path, index, port=args.port, open_browser=not args.no_open,
              gender=args.gender, workshop_dir=workshop, cache_path=cache)
        return

    if args.cmd == "report":
        out = args.out or os.path.join(here, "Uniform绑定总览.html")
        path = build_report(pool_path, out, index, gender=args.gender)
        print("已生成: %s" % path)
        return

    if args.cmd == "templates":
        q = (args.search or "").lower()
        p = Pool(pool_path)
        bound = {}
        for u in p.uniforms():
            for t in u["templates"]:
                bound.setdefault(t.lower(), []).append(display_name(u))
        known_tpls = load_known_templates()
        rows = []
        for k, ent in index.items():
            name = ent["template"]
            disp = ent.get("display") or pretty_template(name)
            if args.preset and not preset_pass(
                    args.preset, known=name.lower() in known_tpls,
                    avail=avail_any(ent, args.gender)):
                continue
            if q and q not in name.lower() and q not in disp.lower() \
                    and q not in (source_label(index, name) or "").lower():
                continue
            rows.append((name, disp, source_label(index, name) or "—",
                         "、".join(bound.get(k, [])) or ""))
        rows.sort(key=lambda r: r[0].lower())
        print("%-34s %-26s %-40s %s" % ("模板", "显示名", "来源 mod", "绑定"))
        for r in rows[:200]:
            print("%-34s %-26s %-40s %s" % (r[0][:34], r[1][:26], r[2][:40], r[3][:40]))
        print("\n共 %d 条" % len(rows))
        return

    if args.cmd == "status":
        if not args.value:
            raise ValueError("status 需要 --value NotUniform|Manual|AnyClass|ClassSpecific|NonSoldier")
        val = STATUS_BY_ARG.get(args.value.lower().replace("_", "").replace("eus", ""))
        if not val:
            raise ValueError("未知状态: %s" % args.value)
        do_bind(pool_path, args.uniform, [], status=val)
        return

    if not args.uniform:
        raise ValueError("%s 需要 --uniform" % args.cmd)
    if not args.template:
        raise ValueError("%s 需要至少一个 --template" % args.cmd)
    do_bind(pool_path, args.uniform, args.template, remove=(args.cmd == "unbind"),
            set_status=args.set_status)


if __name__ == "__main__":
    main()
