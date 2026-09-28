"""
Adhan Connect — Générateur de fiches PDF pour enfants.
Une fiche par lettre arabe avec :
 - Lettre en gros (250 pt) en or
 - Mot exemple (animal) en arabe + français
 - Illustration cartoon vectorielle (dessinée en ReportLab Canvas / SVG paths)
 - Ligne pointillée de traçage de la lettre (3 fois)
 - Bismillah en pied de page + branding Adhan Connect

Toutes les illustrations sont en SVG inline dessinées via ReportLab primitives.
"""

import io
from typing import Callable

from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor, white, Color
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont


# ────────────────────────────────────────────────────────────────────────────
# Palette (cohérente avec l'app)
# ────────────────────────────────────────────────────────────────────────────
GOLD = HexColor("#C9A84C")
GOLD_LIGHT = HexColor("#E8C96A")
GREEN = HexColor("#1B5E3B")
GREEN_LIGHT = HexColor("#3CB371")
IVORY = HexColor("#F8F6F0")
DARK = HexColor("#0D0D12")
SOFT_BG = HexColor("#FFF5E6")  # crème dyslexie-friendly


# ────────────────────────────────────────────────────────────────────────────
# 28 illustrations cartoon vectorielles — chaque fonction dessine sur canvas
# centré sur (cx, cy) avec une taille de base s (radius logique)
# ────────────────────────────────────────────────────────────────────────────
def draw_lion(c, cx, cy, s):
    """🦁 Lion stylisé"""
    # Crinière (cercles dorés autour)
    c.setFillColor(GOLD)
    for i in range(12):
        import math
        ang = i * (math.pi * 2 / 12)
        x = cx + math.cos(ang) * s * 0.95
        y = cy + math.sin(ang) * s * 0.95
        c.circle(x, y, s * 0.25, fill=1, stroke=0)
    # Tête
    c.setFillColor(GOLD_LIGHT)
    c.circle(cx, cy, s * 0.75, fill=1, stroke=0)
    # Yeux
    c.setFillColor(DARK)
    c.circle(cx - s * 0.25, cy + s * 0.1, s * 0.08, fill=1, stroke=0)
    c.circle(cx + s * 0.25, cy + s * 0.1, s * 0.08, fill=1, stroke=0)
    # Museau
    c.setFillColor(HexColor("#8B5A2B"))
    c.ellipse(cx - s * 0.18, cy - s * 0.35, cx + s * 0.18, cy - s * 0.1, fill=1, stroke=0)
    # Bouche
    c.setStrokeColor(DARK)
    c.setLineWidth(2)
    c.line(cx, cy - s * 0.15, cx, cy - s * 0.4)
    c.line(cx, cy - s * 0.4, cx - s * 0.15, cy - s * 0.5)
    c.line(cx, cy - s * 0.4, cx + s * 0.15, cy - s * 0.5)


def draw_cow(c, cx, cy, s):
    """🐄 Vache"""
    # Corps
    c.setFillColor(white)
    c.ellipse(cx - s, cy - s * 0.4, cx + s, cy + s * 0.4, fill=1, stroke=0)
    # Taches noires
    c.setFillColor(DARK)
    c.circle(cx - s * 0.4, cy + s * 0.15, s * 0.25, fill=1, stroke=0)
    c.circle(cx + s * 0.3, cy - s * 0.1, s * 0.2, fill=1, stroke=0)
    c.circle(cx + s * 0.6, cy + s * 0.2, s * 0.18, fill=1, stroke=0)
    # Tête
    c.setFillColor(HexColor("#F5E1C0"))
    c.circle(cx - s * 1.05, cy, s * 0.45, fill=1, stroke=0)
    # Oreilles
    c.setFillColor(HexColor("#E5B58A"))
    c.ellipse(cx - s * 1.4, cy + s * 0.3, cx - s * 1.2, cy + s * 0.55, fill=1, stroke=0)
    # Cornes
    c.setFillColor(GOLD_LIGHT)
    c.rect(cx - s * 1.15, cy + s * 0.4, 4, s * 0.3, fill=1, stroke=0)
    c.rect(cx - s * 1.0, cy + s * 0.4, 4, s * 0.3, fill=1, stroke=0)
    # Yeux
    c.setFillColor(DARK)
    c.circle(cx - s * 1.15, cy + s * 0.1, s * 0.05, fill=1, stroke=0)


def draw_camel(c, cx, cy, s):
    """🐪 Chameau"""
    # Corps avec 2 bosses
    c.setFillColor(HexColor("#D4A574"))
    p = c.beginPath()
    p.moveTo(cx - s, cy)
    p.curveTo(cx - s * 0.7, cy + s * 0.6, cx - s * 0.3, cy + s * 0.4, cx, cy + s * 0.3)
    p.curveTo(cx + s * 0.3, cy + s * 0.6, cx + s * 0.7, cy + s * 0.4, cx + s, cy)
    p.lineTo(cx + s, cy - s * 0.4)
    p.lineTo(cx - s, cy - s * 0.4)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    # Tête
    c.circle(cx - s * 1.1, cy + s * 0.2, s * 0.35, fill=1, stroke=0)
    # Yeux
    c.setFillColor(DARK)
    c.circle(cx - s * 1.2, cy + s * 0.25, s * 0.05, fill=1, stroke=0)


def draw_generic_animal(c, cx, cy, s, color: Color, name: str):
    """Animal générique : grosse ellipse + tête, customisable."""
    c.setFillColor(color)
    c.ellipse(cx - s, cy - s * 0.5, cx + s * 0.6, cy + s * 0.5, fill=1, stroke=0)
    c.circle(cx + s * 0.7, cy + s * 0.2, s * 0.4, fill=1, stroke=0)
    c.setFillColor(DARK)
    c.circle(cx + s * 0.8, cy + s * 0.3, s * 0.06, fill=1, stroke=0)
    # Pattes
    c.setFillColor(color)
    c.rect(cx - s * 0.6, cy - s * 0.7, s * 0.12, s * 0.3, fill=1, stroke=0)
    c.rect(cx + s * 0.2, cy - s * 0.7, s * 0.12, s * 0.3, fill=1, stroke=0)


def draw_fish(c, cx, cy, s):
    """🐟 Poisson"""
    c.setFillColor(GREEN_LIGHT)
    c.ellipse(cx - s, cy - s * 0.4, cx + s * 0.6, cy + s * 0.4, fill=1, stroke=0)
    # Queue
    p = c.beginPath()
    p.moveTo(cx + s * 0.6, cy)
    p.lineTo(cx + s, cy + s * 0.5)
    p.lineTo(cx + s, cy - s * 0.5)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    # Œil
    c.setFillColor(white)
    c.circle(cx - s * 0.6, cy + s * 0.1, s * 0.15, fill=1, stroke=0)
    c.setFillColor(DARK)
    c.circle(cx - s * 0.6, cy + s * 0.1, s * 0.08, fill=1, stroke=0)


def draw_sun(c, cx, cy, s):
    """☀️ Soleil"""
    import math
    c.setFillColor(GOLD)
    # Rayons
    for i in range(12):
        ang = i * (math.pi * 2 / 12)
        x1 = cx + math.cos(ang) * s * 0.7
        y1 = cy + math.sin(ang) * s * 0.7
        x2 = cx + math.cos(ang) * s * 1.1
        y2 = cy + math.sin(ang) * s * 1.1
        c.setStrokeColor(GOLD)
        c.setLineWidth(s * 0.08)
        c.line(x1, y1, x2, y2)
    c.setFillColor(GOLD_LIGHT)
    c.circle(cx, cy, s * 0.6, fill=1, stroke=0)
    # Visage souriant
    c.setFillColor(DARK)
    c.circle(cx - s * 0.2, cy + s * 0.1, s * 0.05, fill=1, stroke=0)
    c.circle(cx + s * 0.2, cy + s * 0.1, s * 0.05, fill=1, stroke=0)
    c.setStrokeColor(DARK)
    c.setLineWidth(s * 0.05)
    c.arc(cx - s * 0.2, cy - s * 0.3, cx + s * 0.2, cy + s * 0.05, 180, 180)


def draw_bird(c, cx, cy, s):
    """🐦 Oiseau"""
    # Corps
    c.setFillColor(HexColor("#5DADE2"))
    c.ellipse(cx - s * 0.7, cy - s * 0.3, cx + s * 0.5, cy + s * 0.5, fill=1, stroke=0)
    # Tête
    c.circle(cx + s * 0.4, cy + s * 0.5, s * 0.3, fill=1, stroke=0)
    # Bec
    c.setFillColor(GOLD)
    p = c.beginPath()
    p.moveTo(cx + s * 0.7, cy + s * 0.5)
    p.lineTo(cx + s * 0.95, cy + s * 0.45)
    p.lineTo(cx + s * 0.7, cy + s * 0.4)
    p.close()
    c.drawPath(p, fill=1, stroke=0)
    # Œil
    c.setFillColor(DARK)
    c.circle(cx + s * 0.45, cy + s * 0.6, s * 0.05, fill=1, stroke=0)
    # Aile
    c.setFillColor(HexColor("#3498DB"))
    c.ellipse(cx - s * 0.3, cy, cx + s * 0.3, cy + s * 0.4, fill=1, stroke=0)


# Mapping lettre → (color, dessin)
ILLUSTRATIONS: dict[str, Callable] = {
    'Alif': lambda c, x, y, s: draw_lion(c, x, y, s),
    'Ba':   lambda c, x, y, s: draw_cow(c, x, y, s),
    'Ta':   lambda c, x, y, s: draw_generic_animal(c, x, y, s, GREEN, 'croco'),
    'Tha':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#E67E22'), 'fox'),
    'Jim':  lambda c, x, y, s: draw_camel(c, x, y, s),
    'Ha':   lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#A0522D'), 'horse'),
    'Kha':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, white, 'sheep'),
    'Dal':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#6B4423'), 'bear'),
    'Dhal': lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#808080'), 'wolf'),
    'Ra':   lambda c, x, y, s: draw_generic_animal(c, x, y, s, white, 'rabbit'),
    'Zay':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, GOLD, 'giraffe'),
    'Sin':  lambda c, x, y, s: draw_fish(c, x, y, s),
    'Shin': lambda c, x, y, s: draw_sun(c, x, y, s),
    'Sad':  lambda c, x, y, s: draw_bird(c, x, y, s),
    'Dad':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, GREEN_LIGHT, 'frog'),
    'Tta':  lambda c, x, y, s: draw_bird(c, x, y, s),
    'Dha':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#D4A574'), 'gazelle'),
    'Ayn':  lambda c, x, y, s: draw_bird(c, x, y, s),
    'Ghayn':lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#D4A574'), 'gazelle'),
    'Fa':   lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#95A5A6'), 'elephant'),
    'Qaf':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#34495E'), 'cat'),
    'Kaf':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#C0392B'), 'dog'),
    'Lam':  lambda c, x, y, s: draw_lion(c, x, y, s),
    'Mim':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, white, 'goat'),
    'Nun':  lambda c, x, y, s: draw_generic_animal(c, x, y, s, HexColor('#E67E22'), 'tiger'),
    'Hha':  lambda c, x, y, s: draw_bird(c, x, y, s),
    'Waw':  lambda c, x, y, s: draw_bird(c, x, y, s),
    'Ya':   lambda c, x, y, s: draw_bird(c, x, y, s),
}


# ────────────────────────────────────────────────────────────────────────────
# Génération d'une fiche
# ────────────────────────────────────────────────────────────────────────────
def render_card(c: canvas.Canvas, ar: str, tr: str, word_ar: str, word_fr: str):
    """Dessine une fiche complète sur le canvas (suppose page courante)."""
    W, H = A4

    # Fond crème
    c.setFillColor(SOFT_BG)
    c.rect(0, 0, W, H, fill=1, stroke=0)

    # Cadre or
    c.setStrokeColor(GOLD)
    c.setLineWidth(4)
    c.rect(20, 20, W - 40, H - 40, fill=0, stroke=1)

    # Bandeau header vert
    c.setFillColor(GREEN)
    c.rect(20, H - 100, W - 40, 80, fill=1, stroke=0)
    # Titre Adhan Connect
    try:
        c.setFont("Helvetica-Bold", 22)
    except Exception:
        c.setFont("Helvetica", 22)
    c.setFillColor(IVORY)
    c.drawCentredString(W / 2, H - 55, "ADHAN CONNECT — Fiche d'apprentissage")
    c.setFont("Helvetica", 12)
    c.setFillColor(GOLD_LIGHT)
    c.drawCentredString(W / 2, H - 80, f"Lettre {tr}  ·  {word_fr}")

    # GROSSE LETTRE (centre haut)
    c.setFillColor(GOLD)
    try:
        c.setFont("Helvetica-Bold", 280)
    except Exception:
        c.setFont("Helvetica", 280)
    c.drawCentredString(W / 2, H - 380, ar)

    # ILLUSTRATION ANIMAL (au milieu de la page)
    illu = ILLUSTRATIONS.get(tr, lambda *_: None)
    illu(c, W / 2, H / 2 - 80, 70)

    # Mot exemple (arabe + français)
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 38)
    c.drawCentredString(W / 2, 220, word_ar)
    c.setFillColor(HexColor("#333333"))
    c.setFont("Helvetica-Oblique", 18)
    c.drawCentredString(W / 2, 195, word_fr)

    # Traçage : 3 lettres pointillées
    c.setStrokeColor(GOLD)
    c.setFillColor(HexColor("#E8C96A"))
    c.setFont("Helvetica", 60)
    c.saveState()
    c.setFillColorRGB(0.9, 0.85, 0.7)
    for i, x in enumerate([W / 4, W / 2, 3 * W / 4]):
        c.drawCentredString(x, 100, ar)
    c.restoreState()
    # Lignes guides
    c.setStrokeColor(HexColor("#D4C49C"))
    c.setLineWidth(1)
    c.setDash(4, 4)
    c.line(W / 4 - 40, 95, W / 4 + 40, 95)
    c.line(W / 2 - 40, 95, W / 2 + 40, 95)
    c.line(3 * W / 4 - 40, 95, 3 * W / 4 + 40, 95)
    c.setDash()

    # Footer Bismillah
    c.setFillColor(GREEN)
    c.setFont("Helvetica", 14)
    c.drawCentredString(W / 2, 50, "بِسْمِ اللَّهِ الرَّحْمَٰنِ الرَّحِيمِ")
    c.setFillColor(HexColor("#666666"))
    c.setFont("Helvetica", 9)
    c.drawCentredString(W / 2, 35, "adhanconnect.com  ·  Apprends l'arabe avec amour")


def make_single_pdf(ar: str, tr: str, word_ar: str, word_fr: str) -> bytes:
    """Génère le PDF d'une seule lettre."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    render_card(c, ar, tr, word_ar, word_fr)
    c.showPage()
    c.save()
    return buf.getvalue()


def make_full_alphabet_pdf(letters: list[dict]) -> bytes:
    """Génère le PDF avec les 28 fiches (une par page)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for l in letters:
        render_card(c, l["ar"], l["tr"], l["word_ar"], l["word_fr"])
        c.showPage()
    c.save()
    return buf.getvalue()


# Les 28 lettres riches (synchro avec frontend)
LETTERS_RICH = [
    {"ar": "ا", "tr": "Alif",  "word_ar": "أَسَد",    "word_fr": "Lion"},
    {"ar": "ب", "tr": "Ba",    "word_ar": "بَقَرَة",   "word_fr": "Vache"},
    {"ar": "ت", "tr": "Ta",    "word_ar": "تِمْسَاح",  "word_fr": "Crocodile"},
    {"ar": "ث", "tr": "Tha",   "word_ar": "ثَعْلَب",   "word_fr": "Renard"},
    {"ar": "ج", "tr": "Jim",   "word_ar": "جَمَل",    "word_fr": "Chameau"},
    {"ar": "ح", "tr": "Ha",    "word_ar": "حِصَان",   "word_fr": "Cheval"},
    {"ar": "خ", "tr": "Kha",   "word_ar": "خَرُوف",   "word_fr": "Mouton"},
    {"ar": "د", "tr": "Dal",   "word_ar": "دُبّ",     "word_fr": "Ours"},
    {"ar": "ذ", "tr": "Dhal",  "word_ar": "ذِئْب",    "word_fr": "Loup"},
    {"ar": "ر", "tr": "Ra",    "word_ar": "رَنَب",    "word_fr": "Lapin"},
    {"ar": "ز", "tr": "Zay",   "word_ar": "زَرَافَة",  "word_fr": "Girafe"},
    {"ar": "س", "tr": "Sin",   "word_ar": "سَمَكَة",  "word_fr": "Poisson"},
    {"ar": "ش", "tr": "Shin",  "word_ar": "شَمْس",    "word_fr": "Soleil"},
    {"ar": "ص", "tr": "Sad",   "word_ar": "صَقْر",    "word_fr": "Faucon"},
    {"ar": "ض", "tr": "Dad",   "word_ar": "ضِفْدَع",  "word_fr": "Grenouille"},
    {"ar": "ط", "tr": "Tta",   "word_ar": "طَائِر",   "word_fr": "Oiseau"},
    {"ar": "ظ", "tr": "Dha",   "word_ar": "ظَبْي",    "word_fr": "Gazelle"},
    {"ar": "ع", "tr": "Ayn",   "word_ar": "عَصْفُور", "word_fr": "Moineau"},
    {"ar": "غ", "tr": "Ghayn", "word_ar": "غَزَال",   "word_fr": "Gazelle"},
    {"ar": "ف", "tr": "Fa",    "word_ar": "فِيل",     "word_fr": "Éléphant"},
    {"ar": "ق", "tr": "Qaf",   "word_ar": "قِطّ",     "word_fr": "Chat"},
    {"ar": "ك", "tr": "Kaf",   "word_ar": "كَلْب",    "word_fr": "Chien"},
    {"ar": "ل", "tr": "Lam",   "word_ar": "لَيْث",    "word_fr": "Lion"},
    {"ar": "م", "tr": "Mim",   "word_ar": "مِعْزَة",  "word_fr": "Chèvre"},
    {"ar": "ن", "tr": "Nun",   "word_ar": "نَمِر",    "word_fr": "Tigre"},
    {"ar": "ه", "tr": "Hha",   "word_ar": "هُدْهُد",   "word_fr": "Huppe"},
    {"ar": "و", "tr": "Waw",   "word_ar": "وَزَّة",   "word_fr": "Oie"},
    {"ar": "ي", "tr": "Ya",    "word_ar": "يَمَامَة",  "word_fr": "Colombe"},
]
