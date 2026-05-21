"""Generate social media infographics from Kahzaabu analysis data."""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path
import textwrap

OUT_DIR = Path(__file__).parent.parent / "data" / "infographics"
OUT_DIR.mkdir(exist_ok=True)

# Fonts
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

def font(size, bold=False):
    try:
        if bold and Path(FONT_BOLD).exists():
            return ImageFont.truetype(FONT_BOLD, size)
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()

# Colors
BG = (10, 10, 18)
WHITE = (255, 255, 255)
RED = (233, 69, 96)
YELLOW = (233, 160, 69)
BLUE = (52, 152, 219)
GREEN = (46, 204, 113)
PURPLE = (155, 89, 182)
GRAY = (120, 120, 130)
DARK = (20, 20, 30)
CARD_BG = (18, 18, 28)

W, H = 1080, 1350  # Instagram portrait


def draw_header(draw, title, subtitle="", y=40):
    """Draw branded header"""
    draw.rectangle([(0, 0), (W, y + 100)], fill=(15, 15, 25))
    draw.line([(0, y + 100), (W, y + 100)], fill=RED, width=3)
    draw.text((W // 2, y + 20), title, fill=WHITE, font=font(38, bold=True), anchor="mt")
    if subtitle:
        draw.text((W // 2, y + 68), subtitle, fill=GRAY, font=font(18), anchor="mt")
    draw.text((W // 2, H - 30), "KAHZAABU — Data from presidency.gov.mv", fill=(60, 60, 70), font=font(14), anchor="mm")
    return y + 120


def draw_stat_row(draw, y, number, label, color=RED):
    draw.text((100, y), str(number), fill=color, font=font(64, bold=True), anchor="lt")
    draw.text((220, y + 18), label, fill=WHITE, font=font(24), anchor="lt")
    return y + 85


def draw_bar(draw, y, label, value, max_val, color, width=600):
    bar_w = int((value / max(max_val, 1)) * width)
    draw.text((80, y), label, fill=GRAY, font=font(16), anchor="lt")
    draw.rectangle([(80, y + 24), (80 + bar_w, y + 48)], fill=color)
    draw.text((90 + bar_w, y + 28), str(value), fill=WHITE, font=font(16), anchor="lt")
    return y + 60


def draw_quote_card(draw, y, quote, source, accent=RED):
    # Card background
    draw.rectangle([(50, y), (W - 50, y + 10)], fill=accent)
    # Find height needed
    wrapped = textwrap.fill(quote, width=48)
    lines = wrapped.split('\n')
    card_h = len(lines) * 28 + 50
    draw.rectangle([(50, y + 10), (W - 50, y + 10 + card_h)], fill=CARD_BG)
    for i, line in enumerate(lines):
        draw.text((80, y + 22 + i * 28), f'"{line}"' if i == 0 else f' {line}"' if i == len(lines) - 1 else f' {line}', fill=WHITE, font=font(19))
    draw.text((80, y + 20 + len(lines) * 28), f"— {source}", fill=GRAY, font=font(14))
    return y + 10 + card_h + 20


# ============================================================
# INFOGRAPHIC 1: The Promise Machine
# ============================================================
def make_promise_machine():
    img = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, "THE PROMISE MACHINE", "What happened to Muizzu's 'this year' promises")

    y += 10
    # Big numbers
    draw.text((W // 2, y), "20", fill=RED, font=font(120, bold=True), anchor="mt")
    y += 130
    draw.text((W // 2, y), "promises with deadlines tracked", fill=GRAY, font=font(20), anchor="mt")
    y += 50

    # Results
    items = [
        (9, "Re-promised the following year", RED),
        (7, "Silently dropped — never mentioned again", YELLOW),
        (3, "Deadline missed, topic still mentioned", BLUE),
        (1, "Actually delivered (Velana Airport — Yameen's project)", GREEN),
    ]
    for num, label, color in items:
        # Circle with number
        cx, cy = 110, y + 25
        draw.ellipse([(cx - 30, cy - 30), (cx + 30, cy + 30)], fill=color)
        draw.text((cx, cy), str(num), fill=WHITE if color != YELLOW else BG, font=font(28, bold=True), anchor="mm")
        # Label
        wrapped = textwrap.fill(label, width=38)
        for i, line in enumerate(wrapped.split('\n')):
            draw.text((160, y + 8 + i * 26), line, fill=WHITE, font=font(20))
        y += max(70, len(wrapped.split('\n')) * 26 + 30)

    y += 20
    draw.line([(80, y), (W - 80, y)], fill=(30, 30, 40), width=1)
    y += 20

    # Key example
    draw.text((80, y), "WORST EXAMPLE:", fill=RED, font=font(16, bold=True))
    y += 30
    y = draw_quote_card(draw, y,
        "1,000 hectares within 8 months — first net-zero carbon eco-city",
        "Muizzu, China visit, Jan 2024", RED)
    draw.text((80, y), "2.5 YEARS LATER:", fill=YELLOW, font=font(16, bold=True))
    y += 28
    draw.text((80, y), "Dropped from Presidential Address entirely.", fill=WHITE, font=font(20))
    y += 28
    draw.text((80, y), "No eco-city. No 1,000 hectares. No explanation.", fill=GRAY, font=font(18))

    img.save(OUT_DIR / "01_promise_machine.png", quality=95)
    print("Saved 01_promise_machine.png")


# ============================================================
# INFOGRAPHIC 2: The India Flip
# ============================================================
def make_india_flip():
    img = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, "THE INDIA FLIP", "From 'India Out' to India's biggest borrower")

    y += 10
    steps = [
        ("NOV 2023", "CAMPAIGN", '"No foreign military\npresence on our soil"', RED),
        ("JAN 2024", "CHINA VISIT", '"China will be our\nclosest partner"', PURPLE),
        ("AUG 2024", "WELCOMES INDIA FM", '"Testament to enhancement\nof historic relations"', BLUE),
        ("OCT 2024", "INDIA STATE VISIT", '"India is a key partner"\nAccepts $400M swap\n+ INR 30 billion', GREEN),
        ("FEB 2025", "PRESIDENTIAL ADDRESS", '8+ categories of\nIndian-funded projects:\nhousing, roads, hospitals,\nairports, schools...', YELLOW),
        ("FEB 2026", "PRESIDENTIAL ADDRESS", 'Thanks India for waiving\nIMF conditionality', YELLOW),
    ]

    for i, (date, event, quote, color) in enumerate(steps):
        # Timeline dot and line
        cx = 90
        cy = y + 35
        if i < len(steps) - 1:
            draw.line([(cx, cy + 15), (cx, cy + 120)], fill=(30, 30, 40), width=2)
        draw.ellipse([(cx - 10, cy - 10), (cx + 10, cy + 10)], fill=color)

        # Date + event
        draw.text((120, y + 8), date, fill=color, font=font(14, bold=True))
        draw.text((220, y + 8), event, fill=GRAY, font=font(13))

        # Quote
        for j, line in enumerate(quote.split('\n')):
            draw.text((120, y + 30 + j * 24), line, fill=WHITE, font=font(18))

        y += max(100, len(quote.split('\n')) * 24 + 55)

    y += 10
    draw.rectangle([(60, y), (W - 60, y + 80)], fill=(30, 10, 10))
    draw.text((W // 2, y + 15), "The man who won on 'India Out'", fill=RED, font=font(22, bold=True), anchor="mt")
    draw.text((W // 2, y + 48), "now depends on India for his country's survival", fill=WHITE, font=font(20), anchor="mt")

    img.save(OUT_DIR / "02_india_flip.png", quality=95)
    print("Saved 02_india_flip.png")


# ============================================================
# INFOGRAPHIC 3: Stolen Credit
# ============================================================
def make_stolen_credit():
    img = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, "STOLEN CREDIT", "Projects built by others, claimed by Muizzu")

    y += 10
    projects = [
        ("Velana Airport Terminal", "YAMEEN", "contracted 2016", "MUIZZU", "inaugurated 2025", BLUE),
        ("Hanimaadhoo Airport", "SOLIH", "Indian EXIM Bank LoC", "MUIZZU", "inaugurated 2025", YELLOW),
        ("Addu Roads", "SOLIH", "Indian Line of Credit", "MUIZZU", "inaugurated 2025", YELLOW),
        ("Water/Sewer 28 Islands", "SOLIH", "Indian LoC project", "MUIZZU", "handed over 2024", YELLOW),
        ("RTL Ferry System", "SOLIH", "launched 2021-22", "MUIZZU", "claims expansion", YELLOW),
        ("Dharumavantha Hospital", "YAMEEN", "built 25-storey", "MUIZZU", '"upgraded" label', BLUE),
        ("Hiya Housing 7,000", "YAMEEN", "built with China loans", "MUIZZU", "claims rent cuts", BLUE),
    ]

    for proj_name, orig, orig_detail, claimer, claim_detail, orig_color in projects:
        # Project name
        draw.text((80, y), proj_name, fill=WHITE, font=font(22, bold=True))
        y += 32

        # Two columns
        # Left: who built it
        draw.rectangle([(80, y), (520, y + 50)], fill=(10, 20, 10))
        draw.text((90, y + 5), f"{orig}", fill=orig_color, font=font(16, bold=True))
        draw.text((90, y + 26), orig_detail, fill=GRAY, font=font(14))

        # Arrow
        draw.text((535, y + 12), "→", fill=GRAY, font=font(24))

        # Right: who claims it
        draw.rectangle([(570, y), (W - 60, y + 50)], fill=(20, 10, 10))
        draw.text((580, y + 5), f"{claimer}", fill=RED, font=font(16, bold=True))
        draw.text((580, y + 26), claim_detail, fill=GRAY, font=font(14))

        y += 65

    y += 15
    draw.line([(80, y), (W - 80, y)], fill=(30, 30, 40), width=1)
    y += 20

    # The 59 stalled claim
    draw.rectangle([(60, y), (W - 60, y + 120)], fill=CARD_BG)
    draw.rectangle([(60, y), (64, y + 120)], fill=PURPLE)
    draw.text((80, y + 10), "PLUS:", fill=PURPLE, font=font(16, bold=True))
    draw.text((80, y + 35), '"59 previously stalled projects', fill=WHITE, font=font(24, bold=True))
    draw.text((80, y + 65), ' revived and brought to completion"', fill=WHITE, font=font(24, bold=True))
    draw.text((80, y + 98), "No list provided. No explanation of why they were 'stalled.'", fill=GRAY, font=font(15))

    img.save(OUT_DIR / "03_stolen_credit.png", quality=95)
    print("Saved 03_stolen_credit.png")


# ============================================================
# INFOGRAPHIC 4: Shrinking Numbers
# ============================================================
def make_shrinking_numbers():
    img = Image.new('RGB', (W, H), BG)
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, "THE SHRINKING NUMBERS", "Promises that quietly got smaller")

    items = [
        {
            "title": "Housing Units (outside Malé)",
            "rows": [
                ("2025 Address", "12,940 units this year", YELLOW),
                ("2026 Address", "9,175 units 'in various stages'", RED),
            ],
            "verdict": "3,765 UNITS VANISHED (-29%)",
        },
        {
            "title": "Budget Deficit",
            "rows": [
                ("2024 Address", "Inherited MVR 14.5 billion (blames Solih)", BLUE),
                ("2025 Address", "His own 2024 deficit: MVR 13.6 billion", YELLOW),
                ("2026 Address", 'Switches to "5% of GDP"', RED),
            ],
            "verdict": "HIS DEFICIT WAS WORSE. CHANGED THE METRIC.",
        },
        {
            "title": "Ras Malé Eco-City",
            "rows": [
                ("Jan 2024", "1,000 hectares in 8 months", BLUE),
                ("Feb 2025", '"Despite obstacles" — this year', YELLOW),
                ("Feb 2026", "NOT MENTIONED", RED),
            ],
            "verdict": "FLAGSHIP PROJECT SILENTLY ABANDONED",
        },
        {
            "title": "Felivaru Cold Storage",
            "rows": [
                ("2025 Address", '"Completed within this year"', YELLOW),
                ("2026 Address", '"Within the next 15 months"', RED),
            ],
            "verdict": "DEADLINE PUSHED 15+ MONTHS",
        },
    ]

    y += 10
    for item in items:
        draw.text((80, y), item["title"], fill=WHITE, font=font(24, bold=True))
        y += 36
        for label, text, color in item["rows"]:
            draw.text((100, y), label, fill=GRAY, font=font(14))
            draw.text((240, y), text, fill=color, font=font(17))
            y += 28
        draw.text((100, y + 5), item["verdict"], fill=RED, font=font(16, bold=True))
        y += 40
        draw.line([(80, y), (W - 80, y)], fill=(25, 25, 35), width=1)
        y += 20

    img.save(OUT_DIR / "04_shrinking_numbers.png", quality=95)
    print("Saved 04_shrinking_numbers.png")


# ============================================================
# INFOGRAPHIC 5: The Big Scorecard
# ============================================================
def make_scorecard():
    img = Image.new('RGB', (W, 1080), BG)  # Square for Twitter/X
    draw = ImageDraw.Draw(img)
    y = draw_header(draw, "MUIZZU SCORECARD", "2.5 years in office — by his own words")

    y += 20
    stats = [
        ("780", "PROMISES MADE", RED),
        ("71", "DELIVERIES CLAIMED", BLUE),
        ("11:1", "PROMISE-TO-DELIVERY RATIO", YELLOW),
        ("20", "BROKEN DEADLINES", RED),
        ("10", "PROJECTS STOLEN FROM OTHERS", PURPLE),
        ("35", "TIMES HE BLAMED PREVIOUS GOVT", GRAY),
        ("0", "FLAGSHIP PROJECTS COMPLETED", RED),
    ]

    for num, label, color in stats:
        draw.text((120, y), str(num), fill=color, font=font(52, bold=True), anchor="lt")
        draw.text((300, y + 14), label, fill=WHITE, font=font(20), anchor="lt")
        y += 75

    y += 15
    draw.line([(80, y), (W - 80, y)], fill=(30, 30, 40), width=1)
    y += 20

    draw.text((80, y), "His own flagship projects:", fill=GRAY, font=font(16))
    y += 28
    flagships = [
        "Ras Malé Eco-City → DROPPED",
        "Addu Bridge → STILL AT SURVEY STAGE",
        "Gulhifalhu Housing → NOT STARTED",
        "Media Village → NOT BUILT",
    ]
    for f in flagships:
        draw.text((100, y), f"✗  {f}", fill=RED, font=font(19))
        y += 32

    y += 10
    draw.text((W // 2, y + 10), "Source: presidency.gov.mv — his own speeches", fill=(50, 50, 60), font=font(14), anchor="mt")
    draw.text((W // 2, 1080 - 30), "KAHZAABU — Data from presidency.gov.mv", fill=(60, 60, 70), font=font(14), anchor="mm")

    img.save(OUT_DIR / "05_scorecard.png", quality=95)
    print("Saved 05_scorecard.png")


if __name__ == "__main__":
    make_promise_machine()
    make_india_flip()
    make_stolen_credit()
    make_shrinking_numbers()
    make_scorecard()
    print(f"\nAll infographics saved to: {OUT_DIR}")
