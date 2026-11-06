#!/usr/bin/env python3
"""制作和检查微信表情投稿素材；图片生成由外部工具完成。"""
from __future__ import annotations

import argparse
import html
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageSequence, ImageStat

SAMPLE = Image.Resampling.LANCZOS
LIMIT = 500 * 1024
ASSETS = {
    'cover': ('封面图.png', (240, 240), LIMIT, True),
    'icon': ('聊天面板图标.png', (50, 50), 100 * 1024, True),
    'banner': ('详情页横幅.png', (750, 400), LIMIT, False),
    'reward_guide': ('赞赏引导图.png', (750, 560), LIMIT, False),
    'reward_thanks': ('赞赏致谢图.png', (750, 750), LIMIT, False),
}


def config(folder):
    data = json.loads((folder / 'pack.json').read_text(encoding='utf-8'))
    if data.get('mode', 'dynamic') not in ('static', 'dynamic'):
        raise ValueError('mode 必须是 static 或 dynamic')
    if not isinstance(data.get('stickers'), list) or not data['stickers']:
        raise ValueError('pack.json 需要非空的 stickers 列表')
    for item in data['stickers']:
        if not isinstance(item, dict) or not item.get('word') or not item.get('action'):
            raise ValueError('每张表情都需要 word 和 action')
    return data


def fitted(source, dimensions, transparent=True):
    source = source.convert('RGBA')
    canvas = Image.new('RGBA', dimensions, (0, 0, 0, 0) if transparent else (255, 255, 255, 255))
    scaled = ImageOps.contain(source, dimensions, method=SAMPLE)
    origin = ((dimensions[0] - scaled.width) // 2, (dimensions[1] - scaled.height) // 2)
    canvas.alpha_composite(scaled, origin)
    return canvas


def load_frames(folder, number, settings):
    base = f'{number:02d}'
    raw = folder / 'gifs_raw' / (base + '.gif')
    sheet = folder / 'sheets' / (base + '.png')
    result = []
    if raw.is_file():
        with Image.open(raw) as animation:
            for frame in ImageSequence.Iterator(animation):
                result.append((fitted(frame.copy(), (240, 240)), max(20, int(frame.info.get('duration', 100) or 100))))
        if len(result) < 2:
            raise ValueError(f'{raw} 不是多帧 GIF')
    elif sheet.is_file():
        with Image.open(sheet) as original:
            grid = original.convert('RGBA')
        if grid.width != grid.height or grid.width % 4:
            raise ValueError(f'{sheet} 不是等分的正方形 4×4 序列图')
        if grid.getchannel('A').getextrema()[0] == 255:
            raise ValueError(f'{sheet} 没有透明背景，请先处理')
        size = grid.width // 4
        frame_ms = int(settings.get('frame_ms', 120))
        for y in range(4):
            for x in range(4):
                tile = grid.crop((x * size, y * size, (x + 1) * size, (y + 1) * size))
                # 检查贴格边缘的素材，避免未经核对的截断。
                a = tile.getchannel('A')
                if any(a.getpixel(pt) > 128 for pt in [(size//2, 0), (size//2, size-1), (0, size//2), (size-1, size//2)]):
                    print(f'注意 {sheet} 第 {y * 4 + x + 1} 格内容可能贴边，请人工检查。')
                result.append((fitted(tile, (240, 240)), frame_ms))
    else:
        raise FileNotFoundError(f'缺少 {raw} 或 {sheet}')
    return result


def encode_gif(frames, destination):
    """逐帧量化并保留透明像素，若过大逐步降低调色板大小。"""
    for colors in (255, 192, 128, 96, 64, 32):
        palette_frames = []
        for image, _duration in frames:
            alpha = image.getchannel('A')
            indexed = image.convert('RGB').quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
            indexed.paste(255, mask=alpha.point(lambda v: 255 if v < 128 else 0))
            indexed.info['transparency'] = 255
            palette_frames.append(indexed)
        buffer = io.BytesIO()
        palette_frames[0].save(buffer, format='GIF', save_all=True,
                               append_images=palette_frames[1:], loop=0,
                               duration=[duration for _, duration in frames],
                               transparency=255, disposal=2, optimize=False)
        payload = buffer.getvalue()
        if len(payload) <= LIMIT:
            destination.write_bytes(payload)
            return
    raise ValueError(f'{destination.name} 压缩后仍大于 500KB，请缩短动画或减少帧数')


def save_png(image, destination, limit):
    out = io.BytesIO()
    image.save(out, format='PNG', optimize=True)
    if out.tell() > limit:
        out = io.BytesIO()
        image.quantize(colors=192, method=Image.Quantize.FASTOCTREE).save(out, format='PNG', optimize=True)
    if out.tell() > limit:
        raise ValueError(f'{destination.name} 大于 {limit // 1024}KB，请简化图片')
    destination.write_bytes(out.getvalue())


def sticker(folder, data, n, target):
    if data.get('mode', 'dynamic') == 'static':
        src = folder / 'images_raw' / f'{n:02d}.png'
        with Image.open(src) as image:
            save_png(fitted(image, (240, 240)), target, LIMIT)
    else:
        encode_gif(load_frames(folder, n, data), target)


def prompts(folder, data):
    for number, item in enumerate(data['stickers'], 1):
        action = item['action']
        caption = item.get('caption') or ''
        kind = '独立静态 PNG' if data.get('mode') == 'static' else '可循环 GIF；若无法直接生成，改为透明底、从左到右从上到下排列的 4×4 等格动作序列图'
        print(f"{number:02d} {item['word']}\n参考角色形象和风格：{data.get('style', '保持参考图风格')}。")
        print(f'聊天含义：{item["word"]}。动作：{action}。输出{kind}，角色完整，透明背景。')
        print(f'配字或符号：{caption}；字迹清晰，与动作协调，不遮住角色。' if caption else '不要添加文字或水印。')
        print()
    print('配套素材：透明正面封面、透明正面头像图标，以及非透明无文字横幅；均保持角色一致。')


def preview(folder, data):
    directory = folder / 'preview'
    directory.mkdir(exist_ok=True)
    target = directory / ('01.png' if data.get('mode') == 'static' else '01.gif')
    if target.exists():
        raise FileExistsError(f'{target} 已存在，请先归档后再生成')
    sticker(folder, data, 1, target)
    print('首张预览：', target)


def create_previews(folder, data, rendered):
    ext = 'png' if data.get('mode') == 'static' else 'gif'
    cards = []
    columns = 4
    board = Image.new('RGB', (240 * columns, 240 * ((len(rendered) + columns - 1) // columns)), '#e9ecef')
    for n, path in rendered:
        with Image.open(path) as im:
            im.seek(im.n_frames // 2)
            frame = im.convert('RGBA')
        board.paste(frame, (((n-1) % columns) * 240, ((n-1) // columns) * 240), frame)
        word = html.escape(data['stickers'][n-1]['word'])
        cards.append(f'<figure><img src="submit/表情图/{n:02d}.{ext}" alt="{word}"><figcaption>{word}</figcaption></figure>')
    board.save(folder / '预览.png')
    content = '<!doctype html><html lang="zh"><meta charset="utf-8"><title>表情预览</title><style>body{font-family:system-ui;background:#e9ecef}main{display:flex;flex-wrap:wrap}figure{text-align:center}img{width:240px;height:240px}</style><main>' + ''.join(cards) + '</main></html>'
    (folder / '预览.html').write_text(content, encoding='utf-8')


def build(folder, data):
    dest = folder / 'submit'
    if dest.exists():
        raise FileExistsError(f'{dest} 已存在，拒绝覆盖')
    # 检查已有输入，避免处理到一半才报告缺文件。
    missing = []
    for n in range(1, len(data['stickers']) + 1):
        if data.get('mode') == 'static':
            candidates = [folder / 'images_raw' / f'{n:02d}.png']
        else:
            candidates = [folder / 'gifs_raw' / f'{n:02d}.gif', folder / 'sheets' / f'{n:02d}.png']
        if not any(p.is_file() for p in candidates):
            missing.append(str(candidates[0]))
    if missing:
        raise FileNotFoundError('缺少表情素材：' + '、'.join(missing))
    output = dest / '表情图'
    output.mkdir(parents=True)
    rendered = []
    suffix = '.png' if data.get('mode') == 'static' else '.gif'
    for n, entry in enumerate(data['stickers'], 1):
        target = output / f'{n:02d}{suffix}'
        sticker(folder, data, n, target)
        rendered.append((n, target))
        print(f'{n:02d} {entry["word"]}: {target.stat().st_size // 1024}KB')
    for key, (filename, dimensions, limit, transparent) in ASSETS.items():
        src = folder / 'assets_raw' / (key + '.png')
        if src.is_file():
            with Image.open(src) as image:
                save_png(fitted(image, dimensions, transparent), dest / filename, limit)
    create_previews(folder, data, rendered)
    lines = [f'# {data.get("name", "表情专辑")} 上架文案', '',
             f'- 表情名称：{data.get("name", "")}', f'- 表情介绍：{data.get("intro", "")}',
             f'- 版权信息：{data.get("copyright") or "待填写"}', f'- 赞赏引导语：{data.get("reward_line", "")}', '', '| 序号 | 含义词 |', '|---|---|']
    lines += [f'| {i:02d} | {item["word"]} |' for i, item in enumerate(data['stickers'], 1)]
    (folder / '上架文案.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    with zipfile.ZipFile(folder / '交付给客户.zip', 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(dest.rglob('*')):
            if path.is_file():
                bundle.write(path, path.relative_to(folder))
        bundle.write(folder / '上架文案.md', '上架文案.md')
    print('已导出投稿素材、动态预览和压缩包；请运行 check 并人工检查。')


def check(folder, data):
    errors, warnings = [], []
    def fail(s): errors.append(s)
    def warn(s): warnings.append(s)
    if not 8 <= len(data['stickers']) <= 24:
        fail('表情数量须为 8–24 张')
    if not data.get('name') or len(data['name']) > 8 or re.search(r'\s|[，。！？,.!?]', data['name']):
        fail('名称应为 1–8 字，避免空格和标点')
    if len(data.get('intro', '')) > 80: fail('介绍超过 80 字')
    if len(data.get('copyright', '')) > 10: fail('版权信息超过 10 字')
    if not data.get('copyright'): warn('版权信息待填写')
    terms = [entry['word'] for entry in data['stickers']]
    if len(terms) != len(set(terms)): fail('含义词重复')
    if any(len(term) > 4 for term in terms): fail('含义词超过 4 字')
    root = folder / 'submit'
    for key, (filename, dimensions, limit, transparent) in ASSETS.items():
        path = root / filename
        if not path.exists():
            if key in ('cover', 'icon', 'banner'): fail(f'缺少{filename}')
            continue
        with Image.open(path) as image:
            if image.size != dimensions or path.stat().st_size > limit: fail(f'{filename} 尺寸或大小不合规格')
            if transparent and image.convert('RGBA').getchannel('A').getextrema()[0] == 255: warn(f'{filename} 没有透明背景')
    extension = 'png' if data.get('mode') == 'static' else 'gif'
    for n, item in enumerate(data['stickers'], 1):
        path = root / '表情图' / f'{n:02d}.{extension}'
        if not path.exists():
            fail(f'缺少 {path.name}')
            continue
        with Image.open(path) as image:
            if image.size != (240, 240) or path.stat().st_size > LIMIT: fail(f'{path.name} 尺寸或大小不合规格')
            if extension == 'gif':
                if image.n_frames < 2: fail(f'{path.name} 不是动态表情')
                if image.info.get('loop') != 0: warn(f'{path.name} 未设置无限循环')
                image.seek(0); first = image.convert('RGBA')
                image.seek(image.n_frames - 1); last = image.convert('RGBA')
                delta = ImageStat.Stat(ImageChops.difference(first, last)).mean
                if sum(delta) / len(delta) > 20: warn(f'{n:02d} {item["word"]} 首尾帧差异较大，请检查循环')
                if first.getchannel('A').getextrema()[0] == 255: warn(f'{n:02d} 首帧无透明区域，请人工检查')
    for error in errors: print('ERROR', error)
    for warning in warnings: print('WARN', warning)
    print(f'结果：{len(errors)} 个必须改，{len(warnings)} 个需人工查看。还需检查版权、字迹、角色一致性与循环。')
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description='微信表情专辑素材工具')
    parser.add_argument('command', choices=['prompts', 'preview', 'build', 'check'])
    parser.add_argument('folder', type=Path, help='包含 pack.json 的工作目录')
    args = parser.parse_args()
    try:
        data = config(args.folder)
        if args.command == 'check': return check(args.folder, data)
        {'prompts': prompts, 'preview': preview, 'build': build}[args.command](args.folder, data)
    except (OSError, ValueError, KeyError) as exc:
        print('错误：', exc, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
