#!/usr/bin/env python3
"""把角色表情素材整理为微信表情专辑，并提供交付前检查。

用法见 SKILL.md。依赖 Python 3.9+ 和 Pillow。
"""
import argparse
import html
import json
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat, ImageSequence

GRID = 4
GIF_SIZE = 240
MAX_GIF = 500 * 1024
BANNED = ["微信", "wechat", "腾讯", "官方", "认证", "正版"]


# ---------- 生成 ----------

def _sticker_prompt(pack, sticker):
    """围绕聊天意图和角色身份生成动作描述，动态图与静态图分别组织提示。"""
    parts = [
        "根据上传的角色参考图制作微信聊天表情，保持角色身份、发型、衣着、配色和画风一致。",
        f"聊天意图：{sticker['word']}；表情动作：{sticker['action']}。",
        "主体清楚，情绪一眼可辨，布局紧凑，肢体完整，透明背景。",
    ]
    if pack.get("style"):
        parts.append(f"视觉要求：{pack['style']}。")
    if sticker.get("caption"):
        parts.append(f"文字准确写成「{sticker['caption']}」，清晰可读，除此之外不加文字。")
        parts.append("文字、符号与动作配合；不遮挡角色，避免不需要的大块文字底色。")
    else:
        parts.append("不要添加文字、水印或标识。")
    if pack.get("mode", "dynamic") == "static":
        parts.append("输出一张独立的方形静态表情图，不要拼成网格。")
    else:
        parts.append(
            "绘制一张正方形 4×4 等格动画序列图，共 16 个连续动作帧，"
            "按从左到右、从上到下排列，首尾衔接；每格角色比例与位置稳定，"
            "完整位于格内，不跨格，不要网格线、编号或边框。"
        )
    return "\n".join(parts)


ASSET_PROMPTS = {
    "cover": "制作角色正面半身或全身封面，主体清晰、简洁，透明底，无文字、白描边或方框。",
    "icon": "制作角色正面头部图标，简洁易辨认，透明底，无文字、白描边或方框。",
    "banner": "制作横向详情页画面，以角色为主角讲一个小故事；背景活泼、非白色且不透明，不加文字。",
    "reward_guide": "制作与角色风格一致的赞赏引导场景，背景明亮且不透明，不添加联系方式、二维码或文字。",
    "reward_thanks": "制作角色表达感谢的场景，背景明亮且不透明，不添加联系方式、二维码或文字。",
}


def load_pack(pack_dir):
    p = Path(pack_dir)
    pack = json.loads((p / "pack.json").read_text())
    return p, pack


def cmd_prompts(a):
    """为当前模式打印可逐张使用的创作提示词。"""
    p, pack = load_pack(a.pack)
    static = pack.get("mode", "dynamic") == "static"
    for i, s in enumerate(pack["stickers"], 1):
        target = f"images_raw/{i:02d}.png" if static else f"sheets/{i:02d}.png"
        print(f"--- {target} ---\n{_sticker_prompt(pack, s)}\n")
    for name, prompt in ASSET_PROMPTS.items():
        if name.startswith("reward_") and not pack.get("reward_line"):
            continue
        print(f"--- assets_raw/{name}.png ---\n保持参考角色身份和画风。{prompt}\n")


# ---------- 合成 ----------

def _slice(sheet_path):
    s = Image.open(sheet_path).convert("RGBA")
    if s.width != s.height or s.width % GRID or s.height % GRID:
        raise ValueError(f"{sheet_path} 必须是可整除的正方形 4×4 精灵图")
    if s.getchannel("A").getextrema()[0] == 255:
        raise ValueError(f"{sheet_path} 没有透明背景，不能自动切帧")
    w, h = s.width // GRID, s.height // GRID
    frames = [s.crop((x * w, y * h, x * w + w, y * h + h)).resize((GIF_SIZE, GIF_SIZE), Image.LANCZOS) for y in range(GRID) for x in range(GRID)]
    # 内容压到格子边上说明这一格可能被切断
    border = 0
    for y in range(GRID):
        for x in range(GRID):
            c = s.crop((x * w, y * h, x * w + w, y * h + h)).getchannel("A")
            px = c.load()
            edge = sum(px[i, 0] > 128 or px[i, c.height - 1] > 128 for i in range(c.width)) + sum(
                px[0, j] > 128 or px[c.width - 1, j] > 128 for j in range(c.height)
            )
            border = max(border, edge)
    return frames, border


def _encode(frames, path, ms, colors, bg):
    """全局调色板编码。全局调色板让相邻帧的像素能复用，GIF 体积明显更小。"""
    flat = []
    for f in frames:
        base = Image.new("RGBA", f.size, "white")
        base.alpha_composite(f)
        flat.append(base.convert("RGB"))
    strip = Image.new("RGB", (GIF_SIZE * len(flat), GIF_SIZE))
    for i, f in enumerate(flat):
        strip.paste(f, (i * GIF_SIZE, 0))
    pal = strip.quantize(colors=colors - 1 if bg == "transparent" else colors, method=Image.MEDIANCUT, dither=Image.Dither.NONE)
    out = []
    for f, rgb in zip(frames, flat):
        q = rgb.quantize(palette=pal, dither=Image.Dither.NONE)
        if bg == "transparent":
            mask = f.getchannel("A").point(lambda a: 255 if a < 128 else 0)
            q.paste(colors - 1, (0, 0), mask)
        out.append(q)
    kw = {"save_all": True, "append_images": out[1:], "duration": ms, "loop": 0, "optimize": False}
    if bg == "transparent":
        kw.update(transparency=colors - 1, disposal=2)
    out[0].save(path, **kw)


def build_direct_gif(source, path, bg):
    """Normalize a real GIF without distorting aspect ratio; preserve per-frame timing."""
    with Image.open(source) as im:
        frames, durations = [], []
        for frame in ImageSequence.Iterator(im):
            duration = frame.info.get("duration", im.info.get("duration", 120))
            src = ImageOps.contain(frame.convert("RGBA"), (GIF_SIZE, GIF_SIZE), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (GIF_SIZE, GIF_SIZE), (0, 0, 0, 0))
            canvas.alpha_composite(src, ((GIF_SIZE - src.width) // 2, (GIF_SIZE - src.height) // 2))
            frames.append(canvas)
            durations.append(duration)
    if len(frames) < 2:
        raise ValueError(f"{source} 不是多帧 GIF")
    for colors in (128, 96, 64, 48):
        _encode(frames, path, durations, colors, bg)
        if path.stat().st_size <= MAX_GIF:
            return path.stat().st_size, colors, len(frames), 0
    return path.stat().st_size, colors, len(frames), 0


def build_gif(sheet, path, ms, bg, keep=None):
    frames, border = _slice(sheet)
    if keep:  # 循环接不上时只留一段帧，写进 pack.json 才不会被下次 build 覆盖
        frames = frames[keep[0] - 1 : keep[1]]
    for frs, dur in ((frames, ms), (frames[::2], ms * 2)):  # 压不下去再抽一半帧
        for colors in (128, 96, 64, 48):
            _encode(frs, path, dur, colors, bg)
            if path.stat().st_size <= MAX_GIF:
                return path.stat().st_size, colors, len(frs), border
    return path.stat().st_size, colors, len(frs), border


def _fit_png(src, size, dst, limit, keep_alpha):
    im = Image.open(src).convert("RGBA")
    if keep_alpha:
        bbox = im.getchannel("A").point(lambda a: 255 if a > 8 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)
        side = max(im.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
        im = square.resize(size, Image.LANCZOS)
        im.save(dst, optimize=True)
    else:
        im = ImageOps.fit(im.convert("RGB"), size, Image.LANCZOS)
        im.save(dst, optimize=True)
        if dst.stat().st_size > limit:
            im.quantize(colors=200).save(dst, optimize=True)


def cmd_preview(a):
    """只转换第一张，供用户确认；不构建整套投稿素材。"""
    p, pack = load_pack(a.pack)
    mode = pack.get("mode", "dynamic")
    if mode not in ("dynamic", "static"):
        sys.exit("mode 必须是 dynamic 或 static")
    if not pack.get("stickers"):
        sys.exit("pack.json 至少需要第一张表情的描述")
    result = p / "preview" / ("01.png" if mode == "static" else "01.gif")
    if result.exists():
        sys.exit(f"预览文件已存在，不覆盖：{result}；请保存旧版本或使用新工作目录。")
    direct = p / "gifs_raw" / "01.gif"
    sheet = p / "sheets" / "01.png"
    still = p / "images_raw" / "01.png"
    if mode == "static" and not still.exists():
        sys.exit(f"缺少 {still}")
    if mode == "dynamic" and not direct.exists() and not sheet.exists():
        sys.exit(f"缺少 {direct} 或 {sheet}")
    result.parent.mkdir(parents=True, exist_ok=True)
    if mode == "static":
        _fit_png(still, (240, 240), result, MAX_GIF, True)
    elif direct.exists():
        build_direct_gif(direct, result, pack.get("background", "transparent"))
    else:
        build_gif(sheet, result, pack.get("frame_ms", 120), pack.get("background", "transparent"), pack["stickers"][0].get("keep_frames"))
    print(f"首张预览：{result}。请展示给用户并等待确认。")


def cmd_build(a):
    p, pack = load_pack(a.pack)
    sub = p / "submit"
    if sub.exists():
        sys.exit("submit 已存在；为避免覆盖，请先使用新的项目目录或手动备份。")
    if len(pack.get("stickers", [])) not in range(8, 25):
        sys.exit("专辑需 8–24 张；请先补齐 pack.json")
    ms = pack.get("frame_ms", 120)
    bg = pack.get("background", "transparent")
    mode = pack.get("mode", "dynamic")
    if mode not in ("dynamic", "static"):
        sys.exit("mode 必须是 dynamic 或 static")
    missing = []
    for i in range(1, len(pack["stickers"]) + 1):
        if mode == "static":
            if not (p / "images_raw" / f"{i:02d}.png").exists():
                missing.append(f"images_raw/{i:02d}.png")
        elif not (p / "gifs_raw" / f"{i:02d}.gif").exists() and not (p / "sheets" / f"{i:02d}.png").exists():
            missing.append(f"gifs_raw/{i:02d}.gif 或 sheets/{i:02d}.png")
    for name in ("cover", "icon", "banner"):
        if not (p / "assets_raw" / f"{name}.png").exists():
            missing.append(f"assets_raw/{name}.png")
    if missing:
        sys.exit("缺少素材，未创建 submit：\n" + "\n".join(missing))
    gifs = sub / "表情图"
    gifs.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, s in enumerate(pack["stickers"], 1):
        if mode == "static":
            src = p / "images_raw" / f"{i:02d}.png"
            if not src.exists():
                print(f"缺 {src}")
                continue
            dst = gifs / f"{i:02d}.png"
            _fit_png(src, (240, 240), dst, MAX_GIF, True)
            rows.append(dst)
            continue
        sheet = p / "sheets" / f"{i:02d}.png"
        direct = p / "gifs_raw" / f"{i:02d}.gif"
        if not sheet.exists() and not direct.exists():
            print(f"缺 {direct} 或 {sheet}")
            continue
        if direct.exists():
            size, colors, n, border = build_direct_gif(direct, gifs / f"{i:02d}.gif", bg)
        else:
            size, colors, n, border = build_gif(sheet, gifs / f"{i:02d}.gif", ms, bg, s.get("keep_frames"))
        flag = "  ⚠ 内容压到格子边，重点看这张" if border > 8 else ""
        print(f"{i:02d} {s['word']}: {size // 1024}KB {n}帧 {colors}色{flag}")
        rows.append(gifs / f"{i:02d}.gif")
    raw = p / "assets_raw"
    specs = [
        ("cover", (240, 240), "封面图.png", 500, True),
        ("icon", (50, 50), "聊天面板图标.png", 100, True),
        ("banner", (750, 400), "详情页横幅.png", 500, False),
        ("reward_guide", (750, 560), "赞赏引导图.png", 500, False),
        ("reward_thanks", (750, 750), "赞赏致谢图.png", 500, False),
    ]
    for name, size, fn, kb, alpha in specs:
        if (raw / f"{name}.png").exists():
            _fit_png(raw / f"{name}.png", size, sub / fn, kb * 1024, alpha)
    _contact_sheet(rows, pack, p / "预览.png")
    _preview_html(pack, p)
    _listing(pack, p)
    with zipfile.ZipFile(p / "交付给客户.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(sub.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(p))
        z.write(p / "上架文案.md", "上架文案.md")
    print("输出：", sub, "；预览.png / 预览.html / 上架文案.md / 交付给客户.zip")


def _contact_sheet(gifs, pack, dst):
    if not gifs:
        return
    cols = min(4, len(gifs))
    rows = (len(gifs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * GIF_SIZE, rows * GIF_SIZE), "#e9ecef")
    for i, g in enumerate(gifs):
        im = Image.open(g)
        im.seek(im.n_frames // 2)
        fr = im.convert("RGBA")
        sheet.paste(fr, ((i % cols) * GIF_SIZE, (i // cols) * GIF_SIZE), fr)
    sheet.save(dst)


def _preview_html(pack, p):
    cells = "".join(
        f'<figure><img src="submit/表情图/{i:02d}.{"png" if pack.get("mode") == "static" else "gif"}"><figcaption>{html.escape(s["word"])}</figcaption></figure>'
        for i, s in enumerate(pack["stickers"], 1)
    )
    (p / "预览.html").write_text(
        f"""<!doctype html><meta charset="utf-8"><title>{html.escape(pack.get('name','表情包'))}</title>
<style>body{{font:14px -apple-system,sans-serif;margin:24px;background:#ededed}}main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px}}
figure{{margin:0;text-align:center}}img{{width:120px;height:120px;background:#fff;border-radius:8px}}
.dark img{{background:#191919}}button{{margin-bottom:16px}}</style>
<h2>{html.escape(pack.get('name',''))}</h2><button onclick="document.body.classList.toggle('dark')">切换深色聊天背景</button>
<main>{cells}</main>""",
        encoding="utf-8",
    )


def _listing(pack, p):
    lines = [
        f"# {pack.get('name','')} 上架文案\n",
        f"- 表情名称：{pack.get('name','')}",
        f"- 表情介绍：{pack.get('intro','')}",
        f"- 版权信息：{pack.get('copyright','')}",
        f"- 赞赏引导语：{pack.get('reward_line','')}\n",
        "| 序号 | 含义词 |",
        "|---|---|",
    ]
    lines += [f"| {i:02d} | {s['word']} |" for i, s in enumerate(pack["stickers"], 1)]
    (p / "上架文案.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 检查 ----------

def _dhash(im):
    g = im.convert("L").resize((9, 8), Image.LANCZOS)
    px = g.load()
    return sum(1 << (x + y * 8) for y in range(8) for x in range(8) if px[x, y] > px[x + 1, y])


def _flat_frame(im, n):
    im.seek(n)
    fr = im.convert("RGBA")
    base = Image.new("RGBA", fr.size, "white")
    base.alpha_composite(fr)
    return base.convert("RGB")


def _chars(s):
    return len(s)


def cmd_check(a):
    p, pack = load_pack(a.pack)
    sub = p / "submit"
    fails, warns = [], []
    F = fails.append
    W = warns.append

    name, intro, cr, line = pack.get("name", ""), pack.get("intro", ""), pack.get("copyright", ""), pack.get("reward_line", "")
    if not 1 <= _chars(name) <= 8 or re.search(r"[\s，。！？、,.!?]", name):
        F(f"表情名称要 1–8 字、无标点无空格：{name!r}")
    if _chars(intro) > 80:
        F(f"表情介绍超过 80 字：{_chars(intro)}")
    if _chars(cr) > 10:
        F(f"版权信息超过 10 字：{cr!r}")
    if line and not 5 <= _chars(line) <= 15:
        F(f"赞赏引导语要 5–15 字：{line!r}")
    for field, text in (("名称", name), ("介绍", intro), ("引导语", line)):
        for b in BANNED:
            if b.lower() in text.lower():
                W(f"{field}含「{b}」，请人工确认是否涉及不当宣传或侵权")

    stickers = pack["stickers"]
    if not 8 <= len(stickers) <= 24:
        F(f"一套要 8–24 张，现在 {len(stickers)} 张")
    words = [s["word"] for s in stickers]
    for w in words:
        if not w or _chars(w) > 4 or re.search(r"[^\w]", w):
            F(f"含义词要 ≤4 字、不带符号：{w!r}")
    for w in {x for x in words if words.count(x) > 1}:
        F(f"含义词重复：{w}")
    for s in stickers:
        for b in BANNED:
            if b.lower() in (s.get("caption") or "").lower():
                W(f"配字含「{b}」，请人工确认：{s['caption']}")

    hashes = []
    for i, s in enumerate(stickers, 1):
        static = pack.get("mode") == "static"
        g = sub / "表情图" / f"{i:02d}.{'png' if static else 'gif'}"
        tag = f"{i:02d} {s['word']}"
        if not g.exists():
            F(f"{tag} 缺文件")
            continue
        im = Image.open(g)
        if im.format != ("PNG" if static else "GIF"):
            F(f"{tag} 文件格式不符")
        if im.size != (240, 240):
            F(f"{tag} 尺寸 {im.size}")
        kb = g.stat().st_size / 1024
        if kb > 500:
            F(f"{tag} 体积 {kb:.0f}KB 超 500KB")
        if static:
            hashes.append((tag, _dhash(im.convert("RGBA"))))
            continue
        if getattr(im, "n_frames", 1) < 2:
            F(f"{tag} 不是动图")
            continue
        if im.info.get("loop") != 0:
            F(f"{tag} 没设无限循环")
        n = im.n_frames
        first, mid = _flat_frame(im, 0), _flat_frame(im, n // 2)
        diff = ImageStat.Stat(ImageChops.difference(first, mid).convert("L")).mean[0]
        if diff < 1.0:
            W(f"{tag} 动作太小（帧差 {diff:.1f}），可能看着不动")
        last = _flat_frame(im, n - 1)
        if ImageStat.Stat(ImageChops.difference(first, last).convert("L")).mean[0] > 18:
            W(f"{tag} 首尾帧差别大，循环可能跳一下")
        hashes.append((tag, _dhash(mid)))
    for x in range(len(hashes)):
        for y in range(x + 1, len(hashes)):
            d = bin(hashes[x][1] ^ hashes[y][1]).count("1")
            if d < 6:
                W(f"{hashes[x][0]} 和 {hashes[y][0]} 太像（距离 {d}），官方要求差异足够")

    for fn, size, kb, alpha in (
        ("封面图.png", (240, 240), 500, True),
        ("聊天面板图标.png", (50, 50), 100, True),
        ("详情页横幅.png", (750, 400), 500, False),
    ):
        f = sub / fn
        if not f.exists():
            F(f"缺 {fn}")
            continue
        _check_img(f, fn, size, kb, alpha, F, W)
    for fn, size in (("赞赏引导图.png", (750, 560)), ("赞赏致谢图.png", (750, 750))):
        f = sub / fn
        if f.exists():
            _check_img(f, fn, size, 500, False, F, W)
        elif line:
            F(f"写了赞赏引导语但缺 {fn}")

    for x in warns:
        print("WARN", x)
    for x in fails:
        print("FAIL", x)
    print(f"结果：{len(fails)} 个必须改，{len(warns)} 个要人看。")
    print("机器看不了的，交给人：配字有没有错别字、角色有没有变形、横幅有没有文字、权利是否清楚。")
    sys.exit(min(len(fails), 100))


def _check_img(f, fn, size, kb, alpha, F, W):
    im = Image.open(f)
    if im.format != "PNG":
        F(f"{fn} 需要 PNG 格式")
    if im.size != size:
        F(f"{fn} 尺寸 {im.size}，要 {size}")
    if f.stat().st_size > kb * 1024:
        F(f"{fn} 超过 {kb}KB")
    rgba = im.convert("RGBA")
    lo = rgba.getchannel("A").getextrema()[0]
    if alpha and lo > 8:
        F(f"{fn} 要透明背景")
    if not alpha:
        if lo < 250:
            F(f"{fn} 不能用透明背景")
        if min(ImageStat.Stat(rgba.convert("RGB")).mean) > 235:
            W(f"{fn} 背景太接近白色")


# ---------- 入口 ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sp = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, h in (("prompts", cmd_prompts, "打印提示词"), ("preview", cmd_preview, "只生成首张预览"), ("build", cmd_build, "切帧、合 GIF、出投稿包"), ("check", cmd_check, "按微信规范检查")):
        s = sp.add_parser(name, help=h)
        s.add_argument("pack")
        s.set_defaults(fn=fn)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
