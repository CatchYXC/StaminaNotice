"""把体力提醒渲染成图片（复用 XutheringWavesUID 的素材与字体拼一张卡）。

为什么自己画而不是直接调对方的绘图函数
--------------------------------------
对方 ``wutheringwaves_stamina/draw_waves_stamina.py`` 的入口全部依赖 ``Event``
（头像、被 at 的人、bot_id 等），推送场景只有一个 UID 和一条数据库记录，拿不到
``Event``；同时那个模块还依赖 ``WavesStaminaRecord`` 等表。所以这里只**借用素材**
（``wutheringwaves_stamina/texture2d``）和字体（``utils/fonts``），按对方的版式
坐标重新拼一张，视觉上和 ``ww每日`` 的体力卡一致。

版式沿用对方的坐标（1150x850 画布）
----------------------------------
* 背景：``bg.jpg``，右侧贴一张随机立绘（取不到就只留背景）
* 左侧信息条：``main_bar.png``（结晶波片 / 结晶单质 / 活跃度三行，贴在 y=190）
* 底部身份条：``base_info_bg.png``（角色名 + UID，贴在 (40, 570)）
* 顶部是推送专属信息：状态胶囊 + 「结晶波片已达设定阈值」+ 阈值说明
* 结晶波片那条进度条上额外画一根金色阈值刻度，一眼看出「是否真的超了」

降级策略
--------
素材缺失 / 字体缺失 / 立绘损坏都不抛异常：逐级退化为「无立绘」→「纯色底」，
最终返回 None，由调用方改发纯文字。
"""

from __future__ import annotations

import io
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

W, H = 1150, 850

# —— 对方版式坐标（都相对 1150x850 画布）——
INFO_Y = 190
ROW_TEXT_Y = (115, 230, 350)  # 相对 main_bar
ROW_BAR = (  # (x0, y0, x1, y1) 相对 main_bar
    (173, 142, 518, 150),
    (173, 254, 518, 262),
    (173, 374, 518, 382),
)
BADGE_POS = (280, 240)
BASE_POS = (40, 570)
PILE_POS = (550, -150)
PILE_MAX_H = 1200

WHITE = (255, 255, 255, 255)
GREY = (216, 216, 216, 255)
GOLD = (224, 202, 146, 255)
YELLOW = (255, 200, 1, 255)
RED = (255, 77, 79, 255)
DARK = (26, 26, 32, 255)

_AUTO = object()
_FONT_CACHE: Dict[Tuple[str, int], Any] = {}
_PILE_CACHE: Optional[list] = None


@dataclass
class CardData:
    """渲染一张推送卡需要的全部数据。"""

    uid: str
    cur: int
    total: int = 240
    threshold: int = 240
    refresh_ts: int = 0
    role_name: str = ""
    store_cur: Optional[int] = None
    store_total: Optional[int] = None
    live_cur: Optional[int] = None
    live_total: Optional[int] = None
    crazy: bool = False
    crazy_minutes: int = 60
    test: bool = False  # 测试推送：胶囊改成「测试推送」，避免被当成真提醒


# ---------------------------------------------------------------- 素材

def _resample() -> Any:
    return getattr(Image, "Resampling", Image).LANCZOS


def _tex_dir(root: Path) -> Path:
    return Path(root) / "wutheringwaves_stamina" / "texture2d"


def _font_dir(root: Path) -> Path:
    return Path(root) / "utils" / "fonts"


def _font(path: Path, size: int) -> Any:
    key = (str(path), size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(str(path), size)
    except Exception:
        font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def _open_png(path: Path) -> Optional[Image.Image]:
    try:
        with Image.open(path) as im:
            return im.convert("RGBA")
    except Exception:
        return None


def _pile_candidates(pile_dir: Optional[Path]) -> list:
    """随机立绘池。只缓存非空结果，资源后下载也能自动生效。"""
    global _PILE_CACHE
    if _PILE_CACHE:
        return _PILE_CACHE
    pool: list = []
    if pile_dir is not None:
        try:
            pool = [str(p) for p in Path(pile_dir).glob("role_pile_*.png") if p.is_file()]
        except Exception:
            pool = []
    if pool:
        _PILE_CACHE = pool
    return pool


def _load_pile(pile_path: Any, pile_dir: Optional[Path]) -> Optional[Image.Image]:
    if pile_path is None and pile_dir is not None:
        pool = _pile_candidates(pile_dir)
        pile_path = random.choice(pool) if pool else None
    if not pile_path:
        return None
    img = _open_png(Path(pile_path))
    if img is None:
        return None
    if img.height > PILE_MAX_H:
        ratio = PILE_MAX_H / img.height
        img = img.resize((max(1, int(img.width * ratio)), PILE_MAX_H), _resample())
    return img


def _compose_base(root: Path, pile: Optional[Image.Image]) -> Image.Image:
    canvas = Image.new("RGBA", (W, H), (12, 14, 20, 255))
    bg = _open_png(_tex_dir(root) / "bg.jpg")
    if bg is not None:
        canvas.paste(bg.convert("RGB").resize((W, H), _resample()), (0, 0))

    if pile is not None:
        # 与对方一致：立绘贴在右半边，允许超出画布
        canvas.paste(pile, PILE_POS, pile)

    # 左侧压一层渐变，保证叠在风景/立绘上的文字可读
    grad = Image.new("L", (256, 1))
    for x in range(256):
        grad.putpixel((x, 0), int(190 * (1 - x / 255) ** 1.5))
    scrim = Image.new("RGBA", (720, H), (6, 8, 16, 0))
    scrim.putalpha(grad.resize((720, H)))
    canvas.alpha_composite(scrim, (0, 0))
    return canvas


# ---------------------------------------------------------------- 文本

def _text_w(draw: ImageDraw.ImageDraw, text: str, font: Any) -> int:
    try:
        left, _, right, _ = draw.textbbox((0, 0), text, font=font)
        return right - left
    except Exception:
        return len(text) * getattr(font, "size", 12)


def _shadow_text(
    draw: ImageDraw.ImageDraw,
    xy: Tuple[float, float],
    text: str,
    font: Any,
    fill: Tuple[int, int, int, int],
    anchor: str = "lm",
) -> None:
    draw.text((xy[0] + 2, xy[1] + 2), text, font=font, fill=(0, 0, 0, 170), anchor=anchor)
    draw.text(xy, text, font=font, fill=fill, anchor=anchor)


def _ratio(cur: Optional[int], total: Optional[int]) -> float:
    if cur is None or not total:
        return 0.0
    return max(0.0, min(1.0, cur / total))


def _fmt_clock(ts: int) -> str:
    """回满时间的口语化描述：今天/明天 HH:MM:SS，更远的写月.日。"""
    dt = datetime.fromtimestamp(ts)
    now = datetime.now()
    today = now.date()
    if dt.date() == today:
        head = "今天"
    elif dt.date() == today + timedelta(days=1):
        head = "明天"
    else:
        return dt.strftime("%m.%d %H:%M:%S")
    return f"{head} {dt.strftime('%H:%M:%S')}"


def _refresh_text(card: CardData) -> str:
    now = int(time.time())
    if not card.refresh_ts or card.refresh_ts <= now:
        return "结晶波片已满"
    return f"{_fmt_clock(card.refresh_ts)} 回满"


def _is_urgent(card: CardData) -> bool:
    now = int(time.time())
    return not card.refresh_ts or card.refresh_ts <= now or card.refresh_ts - now < 4 * 3600


# ---------------------------------------------------------------- 绘制

def _draw_header(
    draw: ImageDraw.ImageDraw,
    card: CardData,
    font_pill: Any,
    font_title: Any,
    font_sub: Any,
) -> None:
    if card.test:
        accent = GOLD
        label = "测试推送"
        title = "这是一条测试推送"
        sub = f"阈值 {card.threshold} · 当前结晶波片 {card.cur} · 不影响正式计时"
    elif card.crazy:
        accent = RED
        label = "催命提醒"
        title = "结晶波片已达设定阈值"
        sub = f"阈值 {card.threshold} · {card.crazy_minutes} 分钟后仍未清理会再次提醒"
    else:
        accent = GOLD
        label = "体力提醒"
        title = "结晶波片已达设定阈值"
        sub = f"阈值 {card.threshold} · 清完体力后会自动重新计时"

    pill_h = 44
    pad_x = 20
    pill_w = _text_w(draw, label, font_pill) + pad_x * 2
    draw.rounded_rectangle((52, 30, 52 + pill_w, 30 + pill_h), radius=pill_h // 2, fill=accent)
    draw.text(
        (52 + pill_w / 2, 30 + pill_h / 2 + 1),
        label,
        font=font_pill,
        fill=DARK if card.test or not card.crazy else WHITE,
        anchor="mm",
    )

    _shadow_text(draw, (54, 112), title, font_title, WHITE)
    _shadow_text(draw, (56, 158), sub, font_sub, GOLD)


def _draw_row(
    draw: ImageDraw.ImageDraw,
    index: int,
    cur_text: str,
    total_text: str,
    ratio: float,
    color: Tuple[int, int, int, int],
    font_num: Any,
    font_unit: Any,
    threshold_ratio: Optional[float] = None,
) -> None:
    x0, y0, x1, y1 = ROW_BAR[index]
    y_text = INFO_Y + ROW_TEXT_Y[index]
    y0 += INFO_Y
    y1 += INFO_Y

    tw_cur = _text_w(draw, cur_text, font_num)
    tw_total = _text_w(draw, total_text, font_unit) if total_text else 0
    left = x1 - tw_total - tw_cur
    draw.text((left, y_text), cur_text, font=font_num, fill=WHITE if total_text else GREY, anchor="lm")
    if total_text:
        draw.text(
            (left + tw_cur + 2, y_text + 3), total_text, font=font_unit, fill=GREY, anchor="lm"
        )

    if ratio > 0:
        end = x0 + max(7.0, (x1 - x0) * ratio)
        draw.rounded_rectangle((x0, y0, end, y1), radius=4, fill=color)

    if threshold_ratio is not None:
        tx = x0 + (x1 - x0) * max(0.0, min(1.0, threshold_ratio))
        draw.rounded_rectangle((tx - 2, y0 - 7, tx + 2, y1 + 7), radius=2, fill=GOLD)
        draw.ellipse((tx - 5, y0 - 15, tx + 5, y0 - 5), fill=GOLD)


def _draw_badge(draw: ImageDraw.ImageDraw, card: CardData, font: Any) -> None:
    text = _refresh_text(card)
    tw = _text_w(draw, text, font)
    x, y = BADGE_POS
    h = 38
    w = tw + 34
    fill = (186, 55, 42, 240) if _is_urgent(card) else (0, 0, 0, 160)
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=fill)
    draw.text((x + 17, y + h / 2), text, font=font, fill=WHITE, anchor="lm")


def _fit_text(draw: ImageDraw.ImageDraw, text: str, font: Any, max_w: int) -> str:
    """按像素宽度截断，超出加省略号（角色名可能很长）。"""
    if _text_w(draw, text, font) <= max_w:
        return text
    while text and _text_w(draw, text + "…", font) > max_w:
        text = text[:-1]
    return f"{text}…" if text else ""


def _draw_base_info(
    canvas: Image.Image,
    root: Path,
    card: CardData,
    font_name: Any,
    font_uid: Any,
) -> None:
    info = _open_png(_tex_dir(root) / "base_info_bg.png")
    if info is None:
        return
    draw = ImageDraw.Draw(info)
    name = _fit_text(draw, card.role_name or "漂泊者", font_name, 236)
    draw.text((275, 118), name, font=font_name, fill=GREY, anchor="lm")
    draw.text((232, 176), f"UID: {card.uid}", font=font_uid, fill=GOLD, anchor="lm")
    canvas.paste(info, BASE_POS, info)


def _draw_tips(
    draw: ImageDraw.ImageDraw,
    card: CardData,
    font_title: Any,
    font_body: Any,
) -> None:
    """无立绘时补的右下角说明面板。"""
    x0, y0, x1, y1 = 716, 626, 1108, 782
    draw.rounded_rectangle((x0, y0, x1, y1), radius=18, fill=(0, 0, 0, 120))
    draw.rounded_rectangle((x0, y0, x0 + 5, y1), radius=3, fill=RED if card.crazy else GOLD)

    if card.test:
        head = "关于这条消息"
        lines = [
            "调试用的预览，可以直接忽略",
            "正式提醒仍按原计时工作",
        ]
    elif card.crazy:
        head = "催命模式"
        lines = [
            f"每隔 {card.crazy_minutes} 分钟提醒一次",
            "清完体力后自动停止",
        ]
    else:
        head = "接下来"
        lines = [
            "体力每 6 分钟恢复 1 点",
            "满值后不再累积，记得清掉",
        ]

    draw.text((x0 + 26, y0 + 30), head, font=font_title, fill=WHITE, anchor="lm")
    for i, line in enumerate(lines):
        draw.text((x0 + 26, y0 + 74 + i * 34), f"· {line}", font=font_body, fill=GREY, anchor="lm")


def _draw_footer(draw: ImageDraw.ImageDraw, card: CardData, font: Any, tip: Optional[str] = None) -> None:
    text = "鸣潮 · 体力提醒" if not tip else f"鸣潮 · 体力提醒  |  {tip}"
    _shadow_text(draw, (1112, 822), text, font, (255, 255, 255, 175), anchor="rm")


def _to_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92, optimize=True)
    return buf.getvalue()


def render_card(
    card: CardData,
    assets_root: Any = None,
    pile_path: Any = _AUTO,
    pile_dir: Any = _AUTO,
) -> Optional[bytes]:
    """渲染推送卡；``assets_root`` 为 XutheringWavesUID 包目录。

    返回 JPEG 字节；素材全缺时返回 None（调用方改发文字）。
    """
    root = Path(assets_root) if assets_root is not None else _default_root()
    if root is None:
        return None

    pile: Optional[Image.Image] = None
    if pile_path is _AUTO:
        pile = _load_pile(None, pile_dir if pile_dir is not _AUTO else _default_pile_dir())
    elif pile_path is not None:
        pile = _load_pile(pile_path, None)

    fonts = _font_dir(root)
    f_path = fonts / "waves_fonts.ttf"
    if not f_path.is_file():
        f_path = fonts / "arial-unicode-ms-bold.ttf"
    if not f_path.is_file():
        # 没字体就别画了，位图默认字体会把中文糊成方块，直接退回文字推送
        return None

    font_pill = _font(f_path, 24)
    font_title = _font(f_path, 40)
    font_sub = _font(f_path, 22)
    font_num = _font(f_path, 32)
    font_unit = _font(f_path, 22)
    font_badge = _font(f_path, 22)
    font_name = _font(f_path, 30)
    font_uid = _font(f_path, 24)
    font_footer = _font(f_path, 20)

    canvas = _compose_base(root, pile)
    draw = ImageDraw.Draw(canvas)

    _draw_header(draw, card, font_pill, font_title, font_sub)

    info = _open_png(_tex_dir(root) / "main_bar.png")
    if info is not None:
        canvas.paste(info, (0, INFO_Y), info)
        draw = ImageDraw.Draw(canvas)

    reached = card.cur >= card.threshold
    _draw_row(
        draw,
        0,
        str(card.cur),
        f"/{card.total or 240}",
        _ratio(card.cur, card.total),
        RED if reached else YELLOW,
        font_num,
        font_unit,
        threshold_ratio=_ratio(card.threshold, card.total),
    )

    # 后两行数据可能缺（国内服没补到 base_info / 接口没给活跃度）：画 "--" 而不是留空
    if card.store_cur is not None:
        store_ratio = _ratio(card.store_cur, card.store_total)
        _draw_row(
            draw,
            1,
            str(card.store_cur),
            f"/{card.store_total or 0}",
            store_ratio,
            RED if store_ratio > 0.8 else YELLOW,
            font_num,
            font_unit,
        )
    else:
        _draw_row(draw, 1, "--", "", 0.0, GREY, font_num, font_unit)

    if card.live_cur is not None:
        _draw_row(
            draw,
            2,
            str(card.live_cur),
            f"/{card.live_total or 0}",
            _ratio(card.live_cur, card.live_total),
            YELLOW,
            font_num,
            font_unit,
        )
    else:
        _draw_row(draw, 2, "--", "", 0.0, GREY, font_num, font_unit)

    _draw_badge(draw, card, font_badge)
    _draw_base_info(canvas, root, card, font_name, font_uid)

    draw = ImageDraw.Draw(canvas)
    if pile is None:
        # 没有立绘时右下角是空风景，补一块提示面板把版面撑满
        _draw_tips(draw, card, font_sub, font_footer)

    if card.test:
        tip = "测试推送，可忽略"
    elif card.crazy:
        tip = "清完体力后记得回来看看"
    else:
        tip = None
    _draw_footer(draw, card, font_footer, tip)
    return _to_bytes(canvas)


def _default_root() -> Optional[Path]:
    try:
        from .xw_bridge import package_root

        return package_root()
    except Exception:
        return None


def _default_pile_dir() -> Optional[Path]:
    try:
        from .xw_bridge import pile_dir

        return pile_dir()
    except Exception:
        return None


async def build_push_image(card: CardData) -> Optional[bytes]:
    """异步入口：线程池渲染，出错返回 None（调用方退化成文字推送）。"""
    import asyncio

    try:
        from gsuid_core.logger import logger
    except Exception:  # pragma: no cover - 核心缺失时不该走到这里
        logger = None  # type: ignore

    try:
        return await asyncio.to_thread(render_card, card)
    except Exception as e:
        if logger is not None:
            logger.exception(f"[鸣潮体力提醒] 渲染推送图片失败，改发文字：{e}")
        return None
