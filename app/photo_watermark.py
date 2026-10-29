# -*- coding: utf-8 -*-
"""
照片EXIF水印工具
- 批量读取文件夹内照片
- 基于EXIF信息添加水印：拍摄时间 / GPS经纬度(海拔) / 自定义文字
- 字体颜色风格参照 normal1.jpeg（白色粗体无衬线）
- 信息版式参照 1.jpg（标签：值，左侧橙色竖条）
- 支持九宫格自定义位置、覆盖源文件导出（可选备份）
"""
import os
import sys
import shutil
import threading
import queue
import traceback
from dataclasses import dataclass, field
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont, ImageOps

Image.MAX_IMAGE_PIXELS = None  # 无人机/相机大图

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

BAR_COLOR = (254, 193, 50)      # 取样自 1.jpg 的橙色竖条
DEFAULT_TEXT_COLOR = (255, 255, 255)

TIME_FORMATS = [
    ("%Y.%m.%d %H:%M", "2026.08.11 22:03"),
    ("%Y.%m.%d %H:%M:%S", "2026.08.11 22:03:45"),
    ("%Y-%m-%d %H:%M", "2026-08-11 22:03"),
    ("%Y-%m-%d %H:%M:%S", "2026-08-11 22:03:45"),
    ("%Y年%m月%d日 %H:%M", "2026年08月11日 22:03"),
    ("%Y/%m/%d %H:%M", "2026/08/11 22:03"),
]

POSITIONS = {
    "top-left": "左上", "top-center": "上中", "top-right": "右上",
    "middle-left": "左中", "center": "居中", "middle-right": "右中",
    "bottom-left": "左下", "bottom-center": "下中", "bottom-right": "右下",
}

# 常用字体（下拉选择）：显示名 -> 字体文件路径
COMMON_FONTS = [
    ("微软雅黑 Bold（默认）", r"C:\Windows\Fonts\msyhbd.ttc"),
    ("微软雅黑", r"C:\Windows\Fonts\msyh.ttc"),
    ("微软雅黑 Light", r"C:\Windows\Fonts\msyhl.ttc"),
    ("黑体", r"C:\Windows\Fonts\simhei.ttf"),
    ("宋体 Bold", r"C:\Windows\Fonts\simsunb.ttf"),
    ("宋体", r"C:\Windows\Fonts\simsun.ttc"),
    ("楷体", r"C:\Windows\Fonts\simkai.ttf"),
    ("仿宋", r"C:\Windows\Fonts\simfang.ttf"),
    ("等线 Bold", r"C:\Windows\Fonts\Dengb.ttf"),
    ("等线", r"C:\Windows\Fonts\Deng.ttf"),
    ("幼圆", r"C:\Windows\Fonts\SIMYOU.TTF"),
    ("隶书", r"C:\Windows\Fonts\SIMLI.TTF"),
    ("华文楷体", r"C:\Windows\Fonts\STKAITI.TTF"),
    ("华文行楷", r"C:\Windows\Fonts\STXINGKA.TTF"),
    ("华文中宋", r"C:\Windows\Fonts\STZHONGS.TTF"),
    ("Arial Bold", r"C:\Windows\Fonts\arialbd.ttf"),
    ("Arial", r"C:\Windows\Fonts\arial.ttf"),
    ("Arial Narrow Bold", r"C:\Windows\Fonts\ARIALNB.TTF"),
]

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",   # 微软雅黑 Bold（最接近示例）
    r"C:\Windows\Fonts\Dengb.ttf",    # 等线 Bold
    r"C:\Windows\Fonts\simhei.ttf",   # 黑体
    r"C:\Windows\Fonts\arialbd.ttf",
]


@dataclass
class WatermarkConfig:
    show_time: bool = True
    time_format: str = "%Y.%m.%d %H:%M"
    show_gps: bool = True
    gps_digits: int = 6
    gps_lon_first: bool = False      # True = 经度在前，纬度在后
    show_altitude: bool = False
    custom_text: str = ""
    position: str = "bottom-left"
    font_scale: float = 1.0          # 1.0 ≈ 短边的 3.1%
    font_path: str = ""
    text_color: tuple = DEFAULT_TEXT_COLOR
    bar_enabled: bool = True
    shadow_enabled: bool = True
    backup_enabled: bool = True
    backup_dirname: str = "_原图备份"
    jpeg_quality: int = 95


# ---------------------------------------------------------------- EXIF 读取

def _ratio_to_float(v):
    try:
        return float(v)
    except Exception:
        try:
            return v.numerator / v.denominator
        except Exception:
            return 0.0


def read_exif_data(path):
    """返回 dict: dt(datetime|None), lat, lon, lat_ref, lon_ref, altitude"""
    data = {"dt": None, "lat": None, "lon": None,
            "lat_ref": "", "lon_ref": "", "altitude": None}
    try:
        img = Image.open(path)
        exif = img.getexif()
        # 时间：优先 DateTimeOriginal
        dt_str = None
        try:
            ifd = exif.get_ifd(0x8769)
            dt_str = ifd.get(0x9003) or ifd.get(0x9004)
        except Exception:
            pass
        if not dt_str:
            dt_str = exif.get(0x0132)  # DateTime
        if dt_str:
            dt_str = str(dt_str).strip()
            for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                        "%Y/%m/%d %H:%M:%S", "%Y:%m:%d %H:%M"):
                try:
                    data["dt"] = datetime.strptime(dt_str, fmt)
                    break
                except ValueError:
                    continue
        # GPS
        try:
            gps = exif.get_ifd(0x8825)
            if gps:
                lat = gps.get(0x0002)
                lon = gps.get(0x0004)
                if lat and len(lat) == 3:
                    data["lat"] = (_ratio_to_float(lat[0]) +
                                   _ratio_to_float(lat[1]) / 60.0 +
                                   _ratio_to_float(lat[2]) / 3600.0)
                    data["lat_ref"] = str(gps.get(0x0001, "N")).strip()
                if lon and len(lon) == 3:
                    data["lon"] = (_ratio_to_float(lon[0]) +
                                   _ratio_to_float(lon[1]) / 60.0 +
                                   _ratio_to_float(lon[2]) / 3600.0)
                    data["lon_ref"] = str(gps.get(0x0003, "E")).strip()
                alt = gps.get(0x0006)
                if alt is not None:
                    a = _ratio_to_float(alt)
                    # GPSAltitudeRef=1 表示低于海平面
                    if gps.get(0x0005, b"\x00") in (1, b"\x01"):
                        a = -a
                    data["altitude"] = a
        except Exception:
            pass
        img.close()
    except Exception:
        pass
    # 无EXIF时间时退回文件修改时间
    if data["dt"] is None:
        try:
            data["dt"] = datetime.fromtimestamp(os.path.getmtime(path))
            data["dt_from_file"] = True
        except Exception:
            pass
    return data


# ---------------------------------------------------------------- 水印绘制

def load_font(font_path, size):
    candidates = [font_path] if font_path else []
    candidates += FONT_CANDIDATES
    for p in candidates:
        if p and os.path.isfile(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


def is_cjk_char(ch):
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or
            0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF or
            0xF900 <= o <= 0xFAFF)


_cjk_support_cache = {}


def font_supports_cjk(path):
    """用 fontTools 检测字体是否包含水印所需的中文字形"""
    if not path:
        return True
    if path in _cjk_support_cache:
        return _cjk_support_cache[path]
    ok = True
    try:
        from fontTools.ttLib import TTFont, TTCollection
        if path.lower().endswith(".ttc"):
            f = TTCollection(path, lazy=True).fonts[0]
        else:
            f = TTFont(path, lazy=True)
        cmap = f.getBestCmap() or {}
        ok = all(ord(c) in cmap for c in "时间经纬度海拔：")
        f.close()
    except Exception:
        ok = True  # 检测失败时按原字体渲染
    _cjk_support_cache[path] = ok
    return ok


def split_runs(text, dual):
    """把文本按 中文/非中文 拆成连续段 [(run_text, is_cjk)]"""
    if not dual or not text:
        return [(text, False)]
    runs = []
    buf = text[0]
    buf_cjk = is_cjk_char(text[0])
    for ch in text[1:]:
        c = is_cjk_char(ch)
        if c == buf_cjk:
            buf += ch
        else:
            runs.append((buf, buf_cjk))
            buf = ch
            buf_cjk = c
    runs.append((buf, buf_cjk))
    return runs


class FontPair:
    """主字体 + 中文回退字体：按字符自动选择，绘制时基线对齐。
    用于西文字体（如 Arial）缺中文字形时，中文自动改用中文字体。"""

    def __init__(self, primary, cjk=None):
        self.primary = primary
        self.cjk = cjk
        self.dual = cjk is not None

    def font_of(self, is_cjk):
        return self.cjk if (is_cjk and self.cjk) else self.primary

    def textlength(self, draw, text):
        return sum(draw.textlength(r, font=self.font_of(c))
                   for r, c in split_runs(text, self.dual))

    def draw_text(self, draw, x, baseline_y, text, fill):
        for r, c in split_runs(text, self.dual):
            f = self.font_of(c)
            draw.text((x, baseline_y), r, font=f, fill=fill, anchor="ls")
            x += draw.textlength(r, font=f)

    def metrics(self):
        a, d = self.primary.getmetrics()
        if self.cjk:
            a2, d2 = self.cjk.getmetrics()
            a, d = max(a, a2), max(d, d2)
        return a, d


def build_lines(cfg, exif_data):
    """生成水印行：[(label, value), ...]，自定义文字 label=None"""
    lines = []
    if cfg.show_time and exif_data.get("dt"):
        lines.append(("时  间", exif_data["dt"].strftime(cfg.time_format)))
    if cfg.show_gps and exif_data.get("lat") is not None and exif_data.get("lon") is not None:
        d = cfg.gps_digits
        lat_s = f"{exif_data['lat']:.{d}f}°{exif_data['lat_ref']}"
        lon_s = f"{exif_data['lon']:.{d}f}°{exif_data['lon_ref']}"
        gps_text = f"{lon_s},{lat_s}" if cfg.gps_lon_first else f"{lat_s},{lon_s}"
        lines.append(("经纬度", gps_text))
    if cfg.show_altitude and exif_data.get("altitude") is not None:
        lines.append(("海  拔", f"{exif_data['altitude']:.1f}m"))
    if cfg.custom_text.strip():
        for t in cfg.custom_text.split("\n"):
            if t.strip():
                lines.append((None, t.strip()))
    return lines


def render_watermark(img, lines, cfg):
    """在 RGB 图像上绘制水印，返回新图像"""
    if not lines:
        return img
    w, h = img.size
    base = min(w, h)
    font_size = max(12, round(base * 0.031 * cfg.font_scale))
    font = load_font(cfg.font_path, font_size)
    # 西文字体缺中文字形时，中文自动回退到默认中文字体
    cjk_font = None
    if cfg.font_path and not font_supports_cjk(cfg.font_path):
        cjk_font = load_font("", font_size)
    fonts = FontPair(font, cjk_font)

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    ascent, descent = fonts.metrics()
    line_h = ascent + descent
    line_gap = round(font_size * 0.42)
    block_h = line_h * len(lines) + line_gap * (len(lines) - 1)

    # 标签对齐：值列起点 = 最宽标签之后
    label_ws = []
    for label, _ in lines:
        if label:
            label_ws.append(fonts.textlength(draw, label + "："))
        else:
            label_ws.append(0)
    max_label_w = max(label_ws) if label_ws else 0
    colon_gap = round(font_size * 0.35)

    value_col_x_off = (max_label_w + colon_gap) if max_label_w > 0 else 0

    # 各行总宽度
    line_ws = []
    for (label, value), lw in zip(lines, label_ws):
        if label:
            line_ws.append(value_col_x_off + fonts.textlength(draw, value))
        else:
            line_ws.append(fonts.textlength(draw, value))
    text_block_w = max(line_ws)

    # 竖条几何
    bar_w = round(font_size * 0.28) if cfg.bar_enabled else 0
    bar_text_gap = round(font_size * 0.55) if cfg.bar_enabled else 0
    content_w = bar_w + bar_text_gap + text_block_w

    margin = round(base * 0.022)

    # 位置计算（九宫格）
    pos = cfg.position
    if "left" in pos:
        x0 = margin
    elif "right" in pos:
        x0 = w - margin - content_w
    else:
        x0 = (w - content_w) // 2
    if "top" in pos:
        y0 = margin
    elif "bottom" in pos:
        y0 = h - margin - block_h
    else:
        y0 = (h - block_h) // 2

    text_x = x0 + bar_w + bar_text_gap

    # 阴影绘制函数（基线对齐绘制，支持双字体）
    def draw_all(off_x, off_y, color):
        yy = y0
        for (label, value) in lines:
            baseline = yy + ascent
            if label:
                fonts.draw_text(draw, text_x + off_x, baseline + off_y,
                                label + "：", color)
                fonts.draw_text(draw, text_x + value_col_x_off + off_x,
                                baseline + off_y, value, color)
            else:
                fonts.draw_text(draw, text_x + off_x, baseline + off_y,
                                value, color)
            yy += line_h + line_gap

    if cfg.shadow_enabled:
        so = max(1, round(font_size * 0.04))
        draw_all(so, so, (0, 0, 0, 130))
    draw_all(0, 0, tuple(cfg.text_color) + (255,))

    # 橙色竖条（贯穿所有行）
    if cfg.bar_enabled:
        bar_top = y0 + round(font_size * 0.08)
        bar_bottom = y0 + block_h - round(font_size * 0.08)
        if cfg.shadow_enabled:
            so = max(1, round(font_size * 0.04))
            draw.rectangle([x0 + so, bar_top + so, x0 + bar_w + so,
                            bar_bottom + so], fill=(0, 0, 0, 150))
        draw.rectangle([x0, bar_top, x0 + bar_w, bar_bottom],
                       fill=BAR_COLOR + (255,))

    out = Image.alpha_composite(img.convert("RGBA"), overlay)
    return out.convert("RGB")


# ---------------------------------------------------------------- 图片处理

def process_image(path, cfg, backup_root=None):
    """处理单张图片：加水印后覆盖源文件。返回 (ok, message)"""
    ext = os.path.splitext(path)[1].lower()
    try:
        img = Image.open(path)
        raw_exif = img.info.get("exif")
        try:
            exif_obj = img.getexif()
        except Exception:
            exif_obj = None
        dpi = img.info.get("dpi")
        img = ImageOps.exif_transpose(img)  # 按方向旋转，水印落在正确位置
        img = img.convert("RGB")

        exif_data = read_exif_data(path)
        lines = build_lines(cfg, exif_data)
        if not lines:
            img.close()
            return False, "无可写入内容（无EXIF且未设置自定义文字）"
        out = render_watermark(img, lines, cfg)
        img.close()

        # 保存参数
        save_kwargs = {}
        fmt = None
        if ext in (".jpg", ".jpeg"):
            fmt = "JPEG"
            save_kwargs.update(quality=cfg.jpeg_quality, optimize=True)
        elif ext == ".png":
            fmt = "PNG"
        elif ext == ".webp":
            fmt = "WEBP"
            save_kwargs.update(quality=cfg.jpeg_quality)
        elif ext in (".tif", ".tiff"):
            fmt = "TIFF"
        elif ext == ".bmp":
            fmt = "BMP"

        # 保留EXIF（方向置为1，因为已按方向转正）
        if fmt in ("JPEG", "WEBP", "TIFF"):
            try:
                if exif_obj is not None:
                    exif_obj[0x0112] = 1
                    save_kwargs["exif"] = exif_obj.tobytes()
                elif raw_exif:
                    save_kwargs["exif"] = raw_exif
            except Exception:
                if raw_exif:
                    save_kwargs["exif"] = raw_exif
        if dpi:
            save_kwargs["dpi"] = dpi

        # 备份原图
        if backup_root:
            rel = os.path.basename(path)
            dst = os.path.join(backup_root, rel)
            if not os.path.exists(dst):
                os.makedirs(backup_root, exist_ok=True)
                shutil.copy2(path, dst)

        tmp = path + ".wm_tmp" + ext
        out.save(tmp, fmt, **save_kwargs)
        out.close()
        os.replace(tmp, path)  # 原子替换源文件
        return True, "OK"
    except Exception as e:
        try:
            if os.path.exists(path + ".wm_tmp" + ext):
                os.remove(path + ".wm_tmp" + ext)
        except Exception:
            pass
        return False, f"{type(e).__name__}: {e}"


def collect_images(folder, recursive):
    files = []
    if recursive:
        for root, _, names in os.walk(folder):
            for n in sorted(names):
                if os.path.splitext(n)[1].lower() in SUPPORTED_EXTS:
                    files.append(os.path.join(root, n))
    else:
        for n in sorted(os.listdir(folder)):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and os.path.splitext(n)[1].lower() in SUPPORTED_EXTS:
                files.append(p)
    return files


# ---------------------------------------------------------------- GUI

def build_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, colorchooser

    app = tk.Tk()
    app.title("照片EXIF水印工具")
    app.geometry("780x860")
    app.minsize(720, 800)

    cfg = WatermarkConfig()
    state = {
        "folder": tk.StringVar(),
        "recursive": tk.BooleanVar(value=False),
        "files": [],
        "running": False,
        "q": queue.Queue(),
        "text_color": list(DEFAULT_TEXT_COLOR),
    }

    pad = {"padx": 8, "pady": 4}

    # ---- 源文件夹 ----
    frm_src = ttk.LabelFrame(app, text="① 选择照片文件夹")
    frm_src.pack(fill="x", **pad)
    ttk.Entry(frm_src, textvariable=state["folder"]).pack(
        side="left", fill="x", expand=True, padx=8, pady=8)
    ttk.Checkbutton(frm_src, text="包含子文件夹",
                    variable=state["recursive"]).pack(side="left", padx=4)
    lbl_count = ttk.Label(frm_src, text="未扫描")
    lbl_count.pack(side="left", padx=8)

    def pick_folder():
        d = filedialog.askdirectory(title="选择照片文件夹")
        if d:
            state["folder"].set(d)
            scan_folder()

    def scan_folder():
        d = state["folder"].get()
        if not d or not os.path.isdir(d):
            lbl_count.config(text="未扫描")
            state["files"] = []
            return
        state["files"] = collect_images(d, state["recursive"].get())
        lbl_count.config(text=f"找到 {len(state['files'])} 张照片")

    ttk.Button(frm_src, text="浏览…", command=pick_folder).pack(side="left", padx=8)
    state["recursive"].trace_add("write", lambda *_: scan_folder())

    # ---- 水印内容 ----
    frm_items = ttk.LabelFrame(app, text="② 水印内容（信息版式参照示例图）")
    frm_items.pack(fill="x", **pad)

    var_time = tk.BooleanVar(value=True)
    var_gps = tk.BooleanVar(value=True)
    var_alt = tk.BooleanVar(value=False)
    var_custom = tk.BooleanVar(value=True)

    row1 = ttk.Frame(frm_items); row1.pack(fill="x", padx=8, pady=3)
    ttk.Checkbutton(row1, text="拍摄时间", variable=var_time).pack(side="left")
    ttk.Label(row1, text="格式：").pack(side="left", padx=(16, 2))
    cmb_fmt = ttk.Combobox(row1, state="readonly", width=24,
                           values=[f"{ex}" for _, ex in TIME_FORMATS])
    cmb_fmt.current(0)
    cmb_fmt.pack(side="left")

    row2 = ttk.Frame(frm_items); row2.pack(fill="x", padx=8, pady=3)
    ttk.Checkbutton(row2, text="GPS经纬度", variable=var_gps).pack(side="left")
    ttk.Checkbutton(row2, text="包含海拔", variable=var_alt).pack(side="left", padx=(16, 4))
    var_lon_first = tk.BooleanVar(value=False)
    ttk.Checkbutton(row2, text="经度在前，纬度在后",
                    variable=var_lon_first).pack(side="left", padx=(12, 4))
    ttk.Label(row2, text="小数位数：").pack(side="left", padx=(12, 2))
    spn_digits = ttk.Spinbox(row2, from_=2, to=10, width=4)
    spn_digits.set(6)
    spn_digits.pack(side="left")

    row3 = ttk.Frame(frm_items); row3.pack(fill="x", padx=8, pady=3)
    ttk.Checkbutton(row3, text="自定义文字", variable=var_custom).pack(side="left")
    ent_custom = ttk.Entry(row3)
    ent_custom.pack(side="left", fill="x", expand=True, padx=(8, 0))
    ent_custom.insert(0, "")

    # ---- 样式 ----
    frm_style = ttk.LabelFrame(app, text="③ 样式与位置")
    frm_style.pack(fill="x", **pad)

    pos_frame = ttk.Frame(frm_style)
    pos_frame.pack(side="left", padx=12, pady=6)
    ttk.Label(pos_frame, text="位置：").grid(row=0, column=0, rowspan=3, sticky="n")
    var_pos = tk.StringVar(value="bottom-left")
    pos_keys = ["top-left", "top-center", "top-right",
                "middle-left", "center", "middle-right",
                "bottom-left", "bottom-center", "bottom-right"]
    for i, key in enumerate(pos_keys):
        rb = ttk.Radiobutton(pos_frame, text=POSITIONS[key], value=key,
                             variable=var_pos)
        rb.grid(row=i // 3, column=i % 3 + 1, sticky="w", padx=4, pady=2)

    right_style = ttk.Frame(frm_style)
    right_style.pack(side="left", fill="both", expand=True, padx=12)

    ttk.Label(right_style, text="字体大小（相对自动比例）：").pack(anchor="w")
    var_scale = tk.DoubleVar(value=1.0)
    scl = ttk.Scale(right_style, from_=0.4, to=3.0, variable=var_scale,
                    orient="horizontal")
    scl.pack(fill="x")
    lbl_scale = ttk.Label(right_style, text="1.0 ×")
    lbl_scale.pack(anchor="w")
    var_scale.trace_add("write",
                        lambda *_: lbl_scale.config(text=f"{var_scale.get():.1f} ×"))

    row_font = ttk.Frame(right_style); row_font.pack(fill="x", pady=4)
    ttk.Label(row_font, text="字体：").pack(side="left")

    # 仅列出系统中实际存在的常用字体
    avail_fonts = [(name, path) for name, path in COMMON_FONTS
                   if os.path.isfile(path)]
    font_names = [name for name, _ in avail_fonts]
    CUSTOM_FONT_LABEL = "自定义字体…"
    font_path_state = {"path": ""}

    cmb_font = ttk.Combobox(row_font, state="readonly", width=20,
                            values=font_names + [CUSTOM_FONT_LABEL])
    cmb_font.current(0)
    cmb_font.pack(side="left", padx=4)

    def on_font_change(_evt=None):
        sel = cmb_font.get()
        if sel == CUSTOM_FONT_LABEL:
            pick_font()
            return
        for name, path in avail_fonts:
            if name == sel:
                font_path_state["path"] = path
                break

    def pick_font():
        f = filedialog.askopenfilename(
            title="选择字体文件",
            initialdir=r"C:\Windows\Fonts",
            filetypes=[("字体文件", "*.ttf *.ttc *.otf")])
        if f:
            font_path_state["path"] = f
            cmb_font.set(f"自定义：{os.path.basename(f)}")
        elif cmb_font.get() == CUSTOM_FONT_LABEL:
            cmb_font.current(0)
            on_font_change()

    cmb_font.bind("<<ComboboxSelected>>", on_font_change)
    ttk.Button(row_font, text="浏览…", command=pick_font).pack(side="left", padx=2)
    on_font_change()  # 初始化默认字体路径

    row_opt = ttk.Frame(right_style); row_opt.pack(fill="x", pady=4)
    var_bar = tk.BooleanVar(value=True)
    var_shadow = tk.BooleanVar(value=True)
    ttk.Checkbutton(row_opt, text="左侧橙色竖条", variable=var_bar).pack(side="left")
    ttk.Checkbutton(row_opt, text="文字阴影", variable=var_shadow).pack(side="left", padx=8)

    color_box = tk.Canvas(row_opt, width=22, height=22, bg="#ffffff",
                          highlightthickness=1, highlightbackground="#888")
    color_box.pack(side="left", padx=(16, 4))

    def pick_color():
        c = colorchooser.askcolor(color="#ffffff", title="选择文字颜色")
        if c and c[0]:
            state["text_color"] = [int(c[0][0]), int(c[0][1]), int(c[0][2])]
            color_box.config(bg=c[1])

    ttk.Button(row_opt, text="文字颜色", command=pick_color).pack(side="left")

    # ---- 输出 ----
    frm_out = ttk.LabelFrame(app, text="④ 输出方式")
    frm_out.pack(fill="x", **pad)
    row_out1 = ttk.Frame(frm_out); row_out1.pack(fill="x", padx=8, pady=3)
    ttk.Label(row_out1, text="处理后直接覆盖源文件（导出至源文件夹）").pack(side="left")
    var_backup = tk.BooleanVar(value=True)
    ttk.Checkbutton(row_out1, text="覆盖前备份原图到「_原图备份」子文件夹",
                    variable=var_backup).pack(side="left", padx=16)
    row_out2 = ttk.Frame(frm_out); row_out2.pack(fill="x", padx=8, pady=3)
    ttk.Label(row_out2, text="JPEG质量：").pack(side="left")
    var_quality = tk.IntVar(value=95)
    ttk.Scale(row_out2, from_=70, to=100, variable=var_quality,
              orient="horizontal", length=200).pack(side="left", padx=4)
    lbl_q = ttk.Label(row_out2, text="95")
    lbl_q.pack(side="left")
    var_quality.trace_add("write",
                          lambda *_: lbl_q.config(text=f"{int(var_quality.get())}"))

    # ---- 操作区 ----
    frm_act = ttk.Frame(app)
    frm_act.pack(fill="x", **pad)

    progress = ttk.Progressbar(frm_act, mode="determinate")
    progress.pack(fill="x", padx=8, pady=(8, 2))
    lbl_status = ttk.Label(frm_act, text="就绪")
    lbl_status.pack(anchor="w", padx=8)

    txt_log = tk.Text(frm_act, height=10, state="disabled", wrap="none")
    txt_log.pack(fill="both", expand=True, padx=8, pady=4)

    def log(msg):
        txt_log.config(state="normal")
        txt_log.insert("end", msg + "\n")
        txt_log.see("end")
        txt_log.config(state="disabled")

    def collect_cfg():
        cfg.show_time = var_time.get()
        cfg.time_format = TIME_FORMATS[cmb_fmt.current()][0]
        cfg.show_gps = var_gps.get()
        cfg.gps_digits = int(spn_digits.get())
        cfg.gps_lon_first = var_lon_first.get()
        cfg.show_altitude = var_alt.get()
        cfg.custom_text = ent_custom.get() if var_custom.get() else ""
        cfg.position = var_pos.get()
        cfg.font_scale = var_scale.get()
        cfg.font_path = font_path_state["path"]
        cfg.text_color = tuple(state["text_color"])
        cfg.bar_enabled = var_bar.get()
        cfg.shadow_enabled = var_shadow.get()
        cfg.backup_enabled = var_backup.get()
        cfg.jpeg_quality = int(var_quality.get())
        return cfg

    # ---- 预览 ----
    def preview():
        files = state["files"]
        if not files:
            scan_folder()
            files = state["files"]
        if not files:
            messagebox.showwarning("提示", "请先选择包含照片的文件夹")
            return
        c = collect_cfg()
        try:
            img = Image.open(files[0])
            img = ImageOps.exif_transpose(img).convert("RGB")
            exif_data = read_exif_data(files[0])
            lines = build_lines(c, exif_data)
            if not lines:
                messagebox.showwarning("提示", "该照片没有可写入的内容")
                return
            out = render_watermark(img, lines, c)
            # 缩放预览
            max_side = 900
            r = min(max_side / out.width, max_side / out.height, 1.0)
            if r < 1.0:
                out = out.resize((round(out.width * r), round(out.height * r)),
                                 Image.LANCZOS)
            top = tk.Toplevel(app)
            top.title(f"预览 - {os.path.basename(files[0])}")
            from PIL import ImageTk
            tk_img = ImageTk.PhotoImage(out)
            lbl = ttk.Label(top, image=tk_img)
            lbl.image = tk_img
            lbl.pack()
            ttk.Label(top, text=f"水印行：{' / '.join(v for _, v in lines)}",
                      wraplength=880).pack(pady=6)
        except Exception as e:
            messagebox.showerror("预览失败", str(e))

    # ---- 批量处理 ----
    def start():
        if state["running"]:
            return
        folder = state["folder"].get()
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning("提示", "请先选择照片文件夹")
            return
        scan_folder()
        files = state["files"]
        if not files:
            messagebox.showwarning("提示", "文件夹中没有支持的图片文件")
            return
        c = collect_cfg()
        if not (c.show_time or c.show_gps or c.show_altitude or c.custom_text.strip()):
            messagebox.showwarning("提示", "请至少选择一项水印内容")
            return
        msg = f"即将处理 {len(files)} 张照片并覆盖源文件。"
        if c.backup_enabled:
            msg += "\n原图将备份到源文件夹下的「_原图备份」。"
        else:
            msg += "\n未开启备份，原图将被永久覆盖！"
        if not messagebox.askyesno("确认开始", msg):
            return

        state["running"] = True
        btn_start.config(state="disabled")
        btn_preview.config(state="disabled")
        progress.config(maximum=len(files), value=0)
        txt_log.config(state="normal"); txt_log.delete("1.0", "end")
        txt_log.config(state="disabled")

        backup_root = (os.path.join(folder, c.backup_dirname)
                       if c.backup_enabled else None)

        def worker():
            ok_n, fail_n = 0, 0
            for i, p in enumerate(files, 1):
                ok, m = process_image(p, c, backup_root)
                if ok:
                    ok_n += 1
                else:
                    fail_n += 1
                state["q"].put(("progress", i, p, ok, m))
            state["q"].put(("done", ok_n, fail_n))

        threading.Thread(target=worker, daemon=True).start()
        poll_queue()

    def poll_queue():
        try:
            while True:
                item = state["q"].get_nowait()
                if item[0] == "progress":
                    _, i, p, ok, m = item
                    progress["value"] = i
                    name = os.path.basename(p)
                    lbl_status.config(text=f"[{i}/{progress['maximum']}] {name}")
                    log(("✓ " if ok else "✗ ") + name + ("" if ok else f"  ({m})"))
                elif item[0] == "done":
                    _, ok_n, fail_n = item
                    lbl_status.config(
                        text=f"完成：成功 {ok_n} 张，失败 {fail_n} 张")
                    log(f"—— 全部完成：成功 {ok_n}，失败 {fail_n} ——")
                    state["running"] = False
                    btn_start.config(state="normal")
                    btn_preview.config(state="normal")
                    messagebox.showinfo(
                        "处理完成", f"成功 {ok_n} 张，失败 {fail_n} 张。")
                    return
        except queue.Empty:
            pass
        app.after(80, poll_queue)

    btns = ttk.Frame(frm_act)
    btns.pack(pady=4)
    btn_preview = ttk.Button(btns, text="预览效果", command=preview)
    btn_preview.pack(side="left", padx=8)
    btn_start = ttk.Button(btns, text="开始处理", command=start)
    btn_start.pack(side="left", padx=8)

    app.mainloop()


# ---------------------------------------------------------------- CLI 自检

def run_selftest(src_folder, font_path="", lon_first=False):
    """无界面自检：处理文件夹内所有图片（用于验证打包后的 exe）
    无控制台的 exe 模式下 print 可能不可见，同时写日志文件。
    可选参数：font_path 测试指定字体，lon_first 测试经度在前。"""
    log_path = os.path.join(src_folder, "_selftest_log.txt")
    log_lines = []

    def safe_print(msg):
        log_lines.append(msg)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
        try:
            if sys.stdout is not None:
                print(msg)
        except Exception:
            pass

    cfg = WatermarkConfig(backup_enabled=False, font_path=font_path,
                          gps_lon_first=lon_first)
    files = collect_images(src_folder, recursive=False)
    safe_print(f"[selftest] {len(files)} images in {src_folder} "
               f"font={font_path or 'default'} lon_first={lon_first}")
    ok_n = 0
    for p in files:
        exif_data = read_exif_data(p)
        lines = build_lines(cfg, exif_data)
        safe_print(f"[selftest] {os.path.basename(p)} lines={[v for _, v in lines]}")
        ok, msg = process_image(p, cfg, backup_root=None)
        safe_print(f"[selftest] -> {'OK' if ok else 'FAIL ' + msg}")
        ok_n += 1 if ok else 0
    safe_print(f"[selftest] done {ok_n}/{len(files)}")
    return 0 if ok_n == len(files) and files else 1


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        fp = sys.argv[3] if len(sys.argv) >= 4 else ""
        lf = len(sys.argv) >= 5 and sys.argv[4] == "lonfirst"
        sys.exit(run_selftest(sys.argv[2], fp, lf))
    try:
        build_gui()
    except Exception:
        traceback.print_exc()
        if sys.stdout is None:  # 无控制台时写入日志文件
            with open(os.path.join(os.path.expanduser("~"),
                                   "photo_watermark_error.log"), "w",
                      encoding="utf-8") as f:
                f.write(traceback.format_exc())
