import os
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

BASE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(BASE, "assets", "fonts")
LOGO = os.path.join(BASE, "assets", "logo_white.png")

W = H = 1080
PAD = 72
DHAKA = timezone(timedelta(hours=6))

CYAN_TOP = (8, 145, 178)
CYAN_BOTTOM = (6, 182, 212)
NAVY = (11, 18, 32)
WHITE = (255, 255, 255)
SOFT_WHITE = (224, 247, 252)
RED = (220, 38, 38)
AMBER = (245, 158, 11)

BN_DIGITS = str.maketrans("0123456789", "০১২৩৪৫৬৭৮৯")
BN_MONTHS = ["জানুয়ারি", "ফেব্রুয়ারি", "মার্চ", "এপ্রিল", "মে", "জুন", "জুলাই", "আগস্ট", "সেপ্টেম্বর", "অক্টোবর", "নভেম্বর", "ডিসেম্বর"]

CATEGORY_COLORS = {
    "flight_disruption": RED,
    "accident": RED,
    "expat_alert": AMBER,
    "labour_rule": AMBER,
    "visa_rule": AMBER,
    "embassy_notice": AMBER,
}


def font(weight, size):
    return ImageFont.truetype(os.path.join(FONTS, f"HindSiliguri-{weight}.ttf"), size, layout_engine=ImageFont.Layout.RAQM)


def bangla_date(dt):
    d = dt.astimezone(DHAKA)
    return f"{str(d.day).translate(BN_DIGITS)} {BN_MONTHS[d.month - 1]} {str(d.year).translate(BN_DIGITS)}"


def wrap(draw, text, fnt, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_text(draw, text, weight, sizes, max_width, max_lines):
    for size in sizes:
        fnt = font(weight, size)
        lines = wrap(draw, text, fnt, max_width)
        if len(lines) <= max_lines:
            return fnt, lines
    fnt = font(weight, sizes[-1])
    lines = wrap(draw, text, fnt, max_width)[:max_lines]
    lines[-1] = lines[-1].rstrip("।,") + "…"
    return fnt, lines


def gradient(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size, top)
    px = img.load()
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            px[x, y] = c
    return img


def render(path, headline, subline, category_label, topic, sources, when=None):
    when = when or datetime.now(timezone.utc)
    img = gradient((W, H), CYAN_TOP, CYAN_BOTTOM)
    draw = ImageDraw.Draw(img)

    ring = tuple(min(255, c + 45) for c in CYAN_TOP)
    for r in (620, 460, 300):
        cx, cy = W - 60, 120
        draw.ellipse((cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2), outline=ring, width=3)

    if os.path.exists(LOGO):
        logo = Image.open(LOGO).convert("RGBA")
        scale = 64 / logo.height
        logo = logo.resize((int(logo.width * scale), 64))
        img.paste(logo, (PAD, PAD - 6), logo)
    else:
        draw.text((PAD, PAD - 10), "EchoFlights", font=font("Bold", 52), fill=WHITE)

    date_font = font("SemiBold", 30)
    date_text = bangla_date(when)
    draw.text((W - PAD - draw.textlength(date_text, font=date_font), PAD + 4), date_text, font=date_font, fill=SOFT_WHITE)

    pill_font = font("Bold", 34)
    pill_w = draw.textlength(category_label, font=pill_font) + 48
    pill_y = 250
    draw.rounded_rectangle((PAD, pill_y, PAD + pill_w, pill_y + 64), radius=32, fill=CATEGORY_COLORS.get(topic, NAVY))
    draw.text((PAD + 24, pill_y + 32), category_label, font=pill_font, fill=WHITE, anchor="lm")

    head_font, head_lines = fit_text(draw, headline, "Bold", (88, 80, 72, 64), W - 2 * PAD, 4)
    y = pill_y + 110
    line_h = int(head_font.size * 1.32)
    for line in head_lines:
        draw.text((PAD, y), line, font=head_font, fill=WHITE)
        y += line_h

    panel_top = 760
    draw.rectangle((0, panel_top, W, H), fill=WHITE)
    draw.rectangle((0, panel_top, 14, H), fill=CATEGORY_COLORS.get(topic, NAVY))

    sub_font, sub_lines = fit_text(draw, subline, "SemiBold", (42, 38, 34), W - 2 * PAD, 3)
    y = panel_top + 40
    for line in sub_lines:
        draw.text((PAD, y), line, font=sub_font, fill=NAVY)
        y += int(sub_font.size * 1.35)

    foot_font = font("Regular", 26)
    src_text = "সূত্র: " + ", ".join(sources)
    if draw.textlength(src_text, font=foot_font) > W - 2 * PAD - 260:
        src_text = "সূত্র: " + ", ".join(sources[:2]) + " ও অন্যান্য"
    draw.text((PAD, H - 56), src_text, font=foot_font, fill=(71, 85, 105), anchor="ls")
    site_font = font("Bold", 28)
    draw.text((W - PAD, H - 56), "echoflights.com", font=site_font, fill=CYAN_TOP, anchor="rs")

    img.save(path, "PNG", optimize=True)
    return path
