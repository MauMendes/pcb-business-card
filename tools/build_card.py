#!/usr/bin/env python3
"""Generate the R16 PCB business card as a KiCad 10 project.

Layout units: mm, board 85 x 55. Run from anywhere: python3 tools/build_card.py
Needs KiCad 10 (pcbnew Python), qrcode, fontTools. Paths below are for Linux
(KiCad footprint library and DejaVu fonts) - adjust LIB / font paths on Windows/macOS.
Run with the system python3 (needs the KiCad 10 `pcbnew` module):

    python3 build_card.py

Conventions
- FRONT (name side) = F.* layers, coordinates = SVG coordinates.
- BACK (reference side) is designed in "back-view" coordinates (as seen when
  looking at the back, exactly like R16_back.svg) and mirrored on placement:
  x_kicad = 85 - x_back.  Library footprints are placed as if on top in the
  back-view frame and then flipped left/right, so seen from the back they look
  exactly like the library footprint seen from the top.
- Gold in R16 = copper + solder-mask opening (ENIG).  White = silkscreen.
"""
import json, math, os, re, subprocess, sys, uuid
import pcbnew
import qrcode
from fontTools.ttLib import TTFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root; script lives in tools/
NAME = 'oss_card'
PCB = os.path.join(HERE, NAME + '.kicad_pcb')
W, H, RC = 85.0, 55.0, 3.0
LIB = '/usr/share/kicad/footprints/'

MM = pcbnew.FromMM
def V(x, y): return pcbnew.VECTOR2I(MM(x), MM(y))

# ------------------------------------------------------------------ fonts --
FONT = 'DejaVu Sans'
_ft = {False: TTFont('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
       True: TTFont('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')}
_cm = {k: f.getBestCmap() for k, f in _ft.items()}
EM_PER_SIZE = 1.399      # KiCad TTF: size 1.0 -> cap height 1.02 mm -> em 1.399 mm
BASE_OFF = 0.17          # KiCad 'bottom' justify sits 0.17*size below the baseline

def text_width(s, size, bold=False, wf=1.0):
    f = _ft[bold]; upm = f['head'].unitsPerEm; hm = f['hmtx']
    adv = sum(hm[_cm[bold].get(ord(c), '.notdef')][0] for c in s)
    return adv / upm * EM_PER_SIZE * size * wf

# text styles (KiCad size = cap height; all silk >= 1.0 mm high, stems >= 0.15 mm)
#   LBL  : small white labels      DejaVu Sans Bold, h 1.0, w 0.8  -> stem ~0.21 mm
#   HD   : section headings        DejaVu Sans Bold, h 1.3
#   HD2  : smaller headings        DejaVu Sans Bold, h 1.2
#   CU   : gold (copper) captions  DejaVu Sans,      h 1.0         -> stem ~0.14 mm
LBL = dict(size=1.0, bold=True, wf=0.8)
HD = dict(size=1.3, bold=True, wf=1.0)
HD2 = dict(size=1.2, bold=True, wf=1.0)
CU = dict(size=1.0, bold=False, wf=1.0)

# ------------------------------------------------------------------ board --
board = pcbnew.BOARD()
ds = board.GetDesignSettings()
ds.SetBoardThickness(MM(0.8))
ds.SetCopperLayerCount(2)

TEXTS = []          # raw s-expr gr_text blocks (TTF faces are written directly)
BOXES = []          # (side, kind, x0, y0, x1, y1, tag) in *view* coords of that side
GROUPS = {}
CU_TAGS = []      # (copper PCB_SHAPE, tag) -> used by the clearance verification copy

def kx(side, x):    # view-x -> KiCad x
    return x if side == 'F' else W - x

def add_shape(shape, group=None):
    board.Add(shape)
    if group:
        GROUPS.setdefault(group, []).append(shape)
    return shape

def L(side, kind):
    return {('F', 'cu'): pcbnew.F_Cu, ('B', 'cu'): pcbnew.B_Cu,
            ('F', 'mask'): pcbnew.F_Mask, ('B', 'mask'): pcbnew.B_Mask,
            ('F', 'silk'): pcbnew.F_SilkS, ('B', 'silk'): pcbnew.B_SilkS}[(side, kind)]

def rect(side, kind, x, y, w, h, group=None, box=True, tag=''):
    """Filled rectangle in view coords. kind 'gold' = copper + mask opening."""
    x0, x1 = sorted((kx(side, x), kx(side, x + w)))
    kinds = ['cu', 'mask'] if kind == 'gold' else [kind]
    for k in kinds:
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_RECT)
        s.SetStart(V(x0, y)); s.SetEnd(V(x1, y + h)); s.SetFilled(True); s.SetWidth(0)
        s.SetLayer(L(side, k)); add_shape(s, group)
        if k == 'cu':
            CU_TAGS.append((s, tag or 'cu'))
    if box:
        BOXES.append((side, 'silk' if kind == 'silk' else 'cu', x, y, x + w, y + h, tag))

def poly(side, kind, pts, group=None):
    kinds = ['cu', 'mask'] if kind == 'gold' else [kind]
    for k in kinds:
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_POLY)
        s.SetPolyPoints([V(kx(side, x), y) for x, y in pts]); s.SetFilled(True); s.SetWidth(0)
        s.SetLayer(L(side, k)); add_shape(s, group)

def circle(side, kind, cx, cy, r, tag=''):
    kinds = ['cu', 'mask'] if kind == 'gold' else [kind]
    for k in kinds:
        s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_CIRCLE)
        s.SetCenter(V(kx(side, cx), cy)); s.SetEnd(V(kx(side, cx) + r, cy)); s.SetFilled(True); s.SetWidth(0)
        s.SetLayer(L(side, k)); add_shape(s)
    BOXES.append((side, 'silk' if kind == 'silk' else 'cu', cx - r, cy - r, cx + r, cy + r, tag))

def hline(side, kind, x0, x1, y, w, tag=''):
    rect(side, kind, x0, y - w / 2, x1 - x0, w, tag=tag)

def text(side, x, y, s, style, kind='silk', anchor='start', tag=None):
    """Text with its *baseline* at y (SVG convention), view coords."""
    size, bold, wf = style['size'], style['bold'], style['wf']
    tw = text_width(s, size, bold, wf)
    x0 = {'start': x, 'middle': x - tw / 2, 'end': x - tw}[anchor]
    desc = 0.33 * size if re.search(r'[gjpqy(),µ]', s) else 0.0
    top = y - (1.06 if re.search(r'[bdfhklt(/0-9A-Z]', s) else 0.75) * size
    BOXES.append((side, 'silk' if kind == 'silk' else 'cu', x0, top, x0 + tw, y + desc, tag or s))
    hj = {'start': 'left', 'middle': '', 'end': 'right'}[anchor]
    if side == 'B':
        hj = {'left': 'left', '': '', 'right': 'right'}[hj]
    just = ' '.join(j for j in (hj, 'bottom', 'mirror' if side == 'B' else '') if j)
    thick = round(max(0.15, size * (0.2 if bold else 0.15)), 3)
    layers = [L(side, 'silk')] if kind == 'silk' else [L(side, 'cu'), L(side, 'mask')]
    for lay in layers:
        lname = {pcbnew.F_Cu: 'F.Cu', pcbnew.B_Cu: 'B.Cu', pcbnew.F_Mask: 'F.Mask', pcbnew.B_Mask: 'B.Mask',
                 pcbnew.F_SilkS: 'F.SilkS', pcbnew.B_SilkS: 'B.SilkS'}[lay]
        TEXTS.append(
            f'\t(gr_text "{s}"\n\t\t(at {kx(side, x):.4f} {y + BASE_OFF * size:.4f} 0)\n'
            f'\t\t(layer "{lname}")\n\t\t(uuid "{uuid.uuid5(uuid.NAMESPACE_URL, f"oss_card/{lname}/{x:.3f}/{y:.3f}/{s}")}")\n'
            f'\t\t(effects\n\t\t\t(font\n\t\t\t\t(face "{FONT}")\n'
            f'\t\t\t\t(size {size:.4f} {size * wf:.4f})\n\t\t\t\t(thickness {thick})\n'
            + ('\t\t\t\t(bold yes)\n' if bold else '') +
            f'\t\t\t)\n\t\t\t(justify {just})\n\t\t)\n\t)\n')
    return tw

# -------------------------------------------------------------- outline --
def edge_line(a, b):
    s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_SEGMENT); s.SetStart(V(*a)); s.SetEnd(V(*b))
    s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(MM(0.05)); board.Add(s)
def edge_arc(c, a0):
    s = pcbnew.PCB_SHAPE(board, pcbnew.SHAPE_T_ARC)
    pts = [(c[0] + RC * math.cos(math.radians(a)), c[1] + RC * math.sin(math.radians(a))) for a in (a0, a0 + 45, a0 + 90)]
    s.SetArcGeometry(V(*pts[0]), V(*pts[1]), V(*pts[2])); s.SetLayer(pcbnew.Edge_Cuts); s.SetWidth(MM(0.05)); board.Add(s)
edge_line((RC, 0), (W - RC, 0)); edge_line((W, RC), (W, H - RC))
edge_line((W - RC, H), (RC, H)); edge_line((0, H - RC), (0, RC))
edge_arc((W - RC, RC), 270); edge_arc((W - RC, H - RC), 0); edge_arc((RC, H - RC), 90); edge_arc((RC, RC), 180)

# ------------------------------------------------------------ footprints --
REFS = {}
def next_ref(prefix):
    REFS[prefix] = REFS.get(prefix, 0) + 1
    return f'{prefix}{REFS[prefix]}'

FPS = []   # (fp, side, label)
def place_lib(lib, name, x, y, rot, prefix, side='B'):
    fp = pcbnew.FootprintLoad(LIB + lib + '.pretty', name)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    fp.SetReference(next_ref(prefix))
    fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
    fp.SetAttributes(pcbnew.FP_SMD | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES | pcbnew.FP_DNP)
    board.Add(fp)
    fp.SetPosition(V(x, y)); fp.SetOrientationDegrees(rot)
    if side == 'B':
        fp.Flip(V(x, y), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        fp.SetPosition(V(W - x, y))
    for p in fp.Pads():
        p.SetNetCode(0)
        bb = p.GetBoundingBox()
        x0, x1 = sorted((kx(side, pcbnew.ToMM(bb.GetLeft())), kx(side, pcbnew.ToMM(bb.GetRight()))))
        BOXES.append((side, 'cu', x0, pcbnew.ToMM(bb.GetTop()), x1, pcbnew.ToMM(bb.GetBottom()), fp.GetReference()))
    FPS.append((fp, side))
    return fp

def pad_boxes(fp, side):
    out = []
    for p in fp.Pads():
        bb = p.GetBoundingBox()
        x0, x1 = sorted((kx(side, pcbnew.ToMM(bb.GetLeft())), kx(side, pcbnew.ToMM(bb.GetRight()))))
        out.append((x0, pcbnew.ToMM(bb.GetTop()), x1, pcbnew.ToMM(bb.GetBottom()), p.GetNumber()))
    return out

def fp_bbox(fp, side):
    b = pad_boxes(fp, side)
    return min(q[0] for q in b), min(q[1] for q in b), max(q[2] for q in b), max(q[3] for q in b)

HOLE_LIB = {}
HOLE_LIB_DIR = os.path.join(HERE, 'oss_card.pretty')
import shutil
shutil.rmtree(HOLE_LIB_DIR, ignore_errors=True)
os.makedirs(HOLE_LIB_DIR)
open(os.path.join(HERE, 'fp-lib-table'), 'w').write('(fp_lib_table\n  (version 7)\n  (lib (name "oss_card")(type "KiCad")(uri "${KIPRJMOD}/oss_card.pretty")(options "")(descr "card holes"))\n)\n')

def hole(x, y, drill, ring, prefix='H', name='Hole'):
    """Plated through hole (both sides). x,y in FRONT (KiCad) coordinates."""
    fp = pcbnew.FOOTPRINT(board)
    fp.SetFPID(pcbnew.LIB_ID('oss_card', name))
    fp.SetReference(next_ref(prefix)); fp.Reference().SetVisible(False)
    fp.SetValue(name); fp.Value().SetVisible(False)
    fp.SetAttributes(pcbnew.FP_THROUGH_HOLE | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
    p = pcbnew.PAD(fp)
    p.SetNumber('1'); p.SetAttribute(pcbnew.PAD_ATTRIB_PTH); p.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_CIRCLE)
    p.SetSize(pcbnew.F_Cu, V(ring, ring)); p.SetDrillSize(V(drill, drill)); p.SetLayerSet(p.PTHMask())
    fp.Add(p)
    if name not in HOLE_LIB:
        HOLE_LIB[name] = fp
        open(os.path.join(HOLE_LIB_DIR, name + '.kicad_mod'), 'w').write(
            f'(footprint "{name}"\n\t(version 20241229)\n\t(generator "build_card")\n\t(layer "F.Cu")\n'
            f'\t(descr "Plated hole, drill {drill} mm, pad {ring} mm (PCB business card)")\n'
            f'\t(property "Reference" "REF**" (at 0 {-ring/2-1:.2f} 0) (layer "F.SilkS") (hide yes) (effects (font (size 1 1) (thickness 0.15))))\n'
            f'\t(property "Value" "{name}" (at 0 {ring/2+1:.2f} 0) (layer "F.Fab") (hide yes) (effects (font (size 1 1) (thickness 0.15))))\n'
            f'\t(attr through_hole exclude_from_pos_files exclude_from_bom)\n'
            f'\t(pad "1" thru_hole circle (at 0 0) (size {ring} {ring}) (drill {drill}) (layers "*.Cu" "*.Mask"))\n)\n')
    board.Add(fp); fp.SetPosition(V(x, y))
    for side in 'FB':
        vx = x if side == 'F' else W - x
        BOXES.append((side, 'cu', vx - ring / 2, y - ring / 2, vx + ring / 2, y + ring / 2, fp.GetReference()))
    return fp

# ================================================================ BACK ====
S = 'B'
# --- top-edge pitch combs (R16: x0 4.5, L 8.6, step L+2.35, fingers y 0.6..2.4)
x = 4.5
for p_, pw, nm in ((0.5, 0.25, 'FFC'), (1.0, 0.5, 'JST SH'), (1.25, 0.6, 'JST GH'), (1.27, 0.6, 'SOIC'),
                   (2.0, 1.0, 'JST PH'), (2.5, 1.2, 'JST XH'), (2.54, 1.2, 'Dupont')):
    Lc = 8.6; n = int(Lc / p_) + 1
    for i in range(n):
        rect(S, 'gold', x + i * p_, 0.6, pw, 1.8, tag=f'comb{p_}')
    cx = x + (n - 1) * p_ / 2 + pw / 2
    text(S, cx, 4.3, f'{p_}', LBL, anchor='middle')
    text(S, cx, 5.75, nm, CU, 'cu', anchor='middle')
    x += Lc + 2.35

# --- RESISTORS / CAPS (chip), vertical, R16 vrow(4.2, 12.9, gap 1.2, minw 2.7, labels y 18.5)
text(S, 4, 8.0, 'RESISTORS / CAPS (chip)', HD)
RES = [('0201', 'R_0201_0603Metric'), ('0402', 'R_0402_1005Metric'), ('0603', 'R_0603_1608Metric'),
       ('0805', 'R_0805_2012Metric'), ('1206', 'R_1206_3216Metric'), ('1210', 'R_1210_3225Metric'),
       ('1812', 'R_1812_4532Metric'), ('2010', 'R_2010_5025Metric'), ('2512', 'R_2512_6332Metric')]

def lib_width(lib, name, rot):
    fp = pcbnew.FootprintLoad(LIB + lib + '.pretty', name)
    fp.SetOrientationDegrees(rot)
    xs = []
    for p in fp.Pads():
        bb = p.GetBoundingBox(); xs += [pcbnew.ToMM(bb.GetLeft()), pcbnew.ToMM(bb.GetRight())]
    return max(xs) - min(xs)

def solve_row(x0, items, gap, lgap=0.35, x1=None):
    """items: (pkg_w, label_w). Returns centers. Like R16 vrow but never lets labels touch.
    If x1 given, spread the slack evenly (R16 'row' behaviour)."""
    cs = []; x = x0
    for i, (w, lw) in enumerate(items):
        if i == 0:
            c = x0 + max(w, lw) / 2
        else:
            pw_, plw = items[i - 1]
            c = cs[-1] + max((pw_ + w) / 2 + gap, (plw + lw) / 2 + lgap)
        cs.append(c)
    if x1 is not None and len(cs) > 1:
        end = cs[-1] + max(items[-1]) / 2
        slack = (x1 - end) / (len(cs) - 1)
        cs = [c + i * slack for i, c in enumerate(cs)]
    return cs

items = [(max(lib_width('Resistor_SMD', fn, 90), 2.7), text_width(n, **LBL)) for n, fn in RES]
for (n, fn), c in zip(RES, solve_row(4.2, items, 1.2)):
    place_lib('Resistor_SMD', fn, c, 12.9, 90, 'R')
    text(S, c, 18.5, n, LBL, anchor='middle')

# --- TRACE -> CURRENT
text(S, 43.5, 8.0, 'TRACE → CURRENT', HD2)
text(S, 43.5, 9.6, 'width mm · 1 oz, +10 °C', CU, 'cu')
y = 11.6
for w_, i_ in ((0.15, '0.6 A'), (0.2, '0.75 A'), (0.3, '1.0 A'), (0.5, '1.45 A'), (1.0, '2.4 A')):
    text(S, 43.5, y + 0.45, f'{w_}', LBL)
    hline(S, 'gold', 47.6, 54.6, y, w_, tag='trace')
    text(S, 55.4, y + 0.45, i_, LBL)
    y += 1.55
text(S, 43.5, 20.0, '1 oz = 35 µm · 0.2 mm ≈ 2.5 mΩ/mm', CU, 'cu')

# --- CLEARANCE (two copper bars per gap; the 0.1 mm pair is grouped for the DRC rule)
CX = 64.0   # R16 63.2; +0.8 so the bigger headings don't touch
text(S, CX, 8.0, 'CLEARANCE', HD2)
text(S, CX, 9.6, 'gap mm · IPC-2221 ext.', CU, 'cu')
for k, (v, g) in enumerate((('≤30 V', 0.1), ('≤150 V', 0.6), ('≤300 V', 1.25), ('≤500 V', 2.5))):
    yy = 11.6 + k * 1.9
    text(S, CX, yy + 0.45, v, LBL)
    grp = 'CLR_DEMO_0p1' if g == 0.1 else None
    # copper bars
    for bx in ((CX + 9.8) - g / 2 - 1.6, (CX + 9.8) + g / 2):
        rect(S, 'cu', bx, yy - 0.4, 1.6, 0.8, group=grp, tag=f'clr{g}')
    # mask: one common opening for the 0.1 mm pair (a 0.1 mm mask web is not
    # manufacturable), separate openings for the wider gaps
    if g == 0.1:
        rect(S, 'mask', (CX + 9.8) - g / 2 - 1.6, yy - 0.4, 3.2 + g, 0.8, box=False)
    else:
        for bx in ((CX + 9.8) - g / 2 - 1.6, (CX + 9.8) + g / 2):
            rect(S, 'mask', bx, yy - 0.4, 1.6, 0.8, box=False)
    text(S, CX + 13.2, yy + 0.45, f'{g}', LBL)

# --- DIODES / TANTALUM / MCU row (centre y 28.2 / 29.4, labels y 35.6)
YC, LY = 28.2, 35.6
DIO = [('523', 'D_SOD-523'), ('323', 'D_SOD-323'), ('123', 'D_SOD-123'), ('123F', 'D_SOD-123F'),
       ('SMA', 'D_SMA'), ('SMB', 'D_SMB'), ('SMC', 'D_SMC')]
TAN = [('A', 'CP_EIA-3216-18_Kemet-A'), ('B', 'CP_EIA-3528-21_Kemet-B'),
       ('C', 'CP_EIA-6032-28_Kemet-C'), ('D', 'CP_EIA-7343-31_Kemet-D')]

def band_above(fp, side):
    """Cathode / + band: 0.25 mm silk line 0.45 mm above the top pad (R16)."""
    pads = pad_boxes(fp, side)
    top = min(pads, key=lambda q: q[1])
    yb = top[1] - 0.45
    rect(side, 'silk', top[0], yb - 0.125, top[2] - top[0], 0.25, tag='band')

text(S, 4, 22.2, 'DIODES', HD)
text(S, 15, 22.2, 'SOD- / SMx', CU, 'cu')
items = [(max(lib_width('Diode_SMD', fn, 270), 2.5), text_width(n, **LBL)) for n, fn in DIO]
cs = solve_row(4.2, items, 0.55, lgap=0.6)
for (n, fn), c in zip(DIO, cs):
    fp = place_lib('Diode_SMD', fn, c, YC, 270, 'D'); band_above(fp, S)
    text(S, c, LY, n, LBL, anchor='middle')
x = cs[-1] + items[-1][0] / 2 + 1.6
text(S, x, 22.2, 'TANTALUM', HD)
text(S, x, 23.8, 'case', CU, 'cu')
xt = x
items = [(max(lib_width('Capacitor_Tantalum_SMD', fn, 270), 2.4), text_width(n, **LBL)) for n, fn in TAN]
cs = solve_row(x + 0.2, items, 0.55)
for (n, fn), c in zip(TAN, cs):
    fp = place_lib('Capacitor_Tantalum_SMD', fn, c, YC, 270, 'C'); band_above(fp, S)
    text(S, c, LY, n, LBL, anchor='middle')
x = max(cs[-1] + items[-1][0] / 2, xt + text_width('TANTALUM', **HD)) + 2.4

# MCU / SMALL ICs (centre y 29.4)
MCU = [('LQFP-48', 'Package_QFP', 'LQFP-48_7x7mm_P0.5mm', 0),
       ('QFN-32', 'Package_DFN_QFN', 'QFN-32-1EP_5x5mm_P0.5mm_EP3.1x3.1mm', 0),
       ('MSOP-8', 'Package_SO', 'MSOP-8_3x3mm_P0.65mm', 0),
       ('SOT-89', 'Package_TO_SOT_SMD', 'SOT-89-3', 90),
       ('SOT-363', 'Package_TO_SOT_SMD', 'SOT-363_SC-70-6', 0),
       ('DFN-8', 'Package_DFN_QFN', 'DFN-8-1EP_2x2mm_P0.5mm_EP0.9x1.6mm', 0)]
text(S, x, 22.2, 'MCU / SMALL ICs', HD)
items = []
for n, lib, fn, rot in MCU:
    lw = text_width(n, **LBL)
    if n == 'LQFP-48':
        lw = 0  # its label sits lower (y 36.4) under the wide package
    items.append((lib_width(lib, fn, rot), lw))
cs = solve_row(x, items, 0.8, x1=81.6)
MCU_FP = {}
for (n, lib, fn, rot), c in zip(MCU, cs):
    MCU_FP[n] = place_lib(lib, fn, c, 29.4, rot, 'U')
    text(S, c, 36.4 if n == 'LQFP-48' else LY, n, LBL, anchor='middle')

# --- HOLES (left) — R16 back-view x, front x = 85 - x
text(S, 4, 37.6, 'HOLES', HD)
HY = 40.6
HOLES = [('M2', 2.2, 3.6, 25.0), ('M2.5', 2.7, 4.4, 17.5), ('M3', 3.2, 5.2, 8.8)]
for n, d, r, hx in HOLES:
    hole(W - hx, HY, d, r, name=f'Hole_{n}_plated_d{d}')
    text(S, hx, HY + r / 2 + 1.5, n, LBL, anchor='middle')

# --- AWG wire-gauge holes (plated). Finished hole = bare wire + 0.05 mm, rounded up to 0.01.
AWG = [(28, 0.32), (26, 0.405), (24, 0.511), (22, 0.644), (20, 0.812), (18, 1.024)]
WY = 46.8; WX = [5.2 + i * 3.9 for i in range(6)]
AWG_DRILL = {}
for (g, d), wx in zip(AWG, WX):
    drill = math.ceil((d + 0.05) * 100 - 1e-6) / 100
    AWG_DRILL[g] = (d, drill, round(d + 0.55, 3))
    hole(W - wx, WY, drill, round(d + 0.55, 3), name=f'Hole_AWG{g}_d{drill}')
    text(S, wx, WY + 2.35, str(g), LBL, anchor='middle')
text(S, WX[-1] + 1.4, WY + 0.4, 'AWG', CU, 'cu')

# --- IC / POWER PACKAGES. Small ICs centre y 42.4 (x 30..), SOT-223 + DPAK (real
# library sizes are much taller than the R16 sketch) on the right, labels lower.
text(S, 30, 39.0, 'IC / POWER PACKAGES', HD)
ICS = [('SOT-23', 'Package_TO_SOT_SMD', 'SOT-23', 0), ('SOT-23-6', 'Package_TO_SOT_SMD', 'SOT-23-6', 0),
       ('SOIC-8', 'Package_SO', 'SOIC-8_3.9x4.9mm_P1.27mm', 0), ('TSSOP-14', 'Package_SO', 'TSSOP-14_4.4x5mm_P0.65mm', 0),
       ('QFN-16', 'Package_DFN_QFN', 'QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm', 0)]
PWR = [('SOT-223', 'Package_TO_SOT_SMD', 'SOT-223-3_TabPin2', 90), ('DPAK', 'Package_TO_SOT_SMD', 'TO-252-2', 90)]
pw_items = [(lib_width(l, f, r), text_width(n, **LBL)) for n, l, f, r in PWR]
# DPAK flush right at 81.6, SOT-223 left of it
dpak_c = 81.6 - pw_items[1][0] / 2
sot223_c = dpak_c - max((pw_items[0][0] + pw_items[1][0]) / 2 + 1.9, (pw_items[0][1] + pw_items[1][1]) / 2 + 0.6)
items = [(lib_width(l, f, r), text_width(n, **LBL)) for n, l, f, r in ICS]
x1 = sot223_c - max(pw_items[0]) / 2 - 1.4
cs = solve_row(30.0, items, 0.8, x1=x1)
IC_FP = {}
for (n, lib, fn, rot), c in zip(ICS, cs):
    IC_FP[n] = place_lib(lib, fn, c, 42.4, rot, 'U')
    text(S, c, 46.3, n, LBL, anchor='middle')
# SOT-223: tab on top, pins below
fp = place_lib(*PWR[0][1:3], sot223_c, 0, 90, 'U')
bb = fp_bbox(fp, S); fp.SetPosition(V(W - sot223_c, 39.9 - bb[1])); IC_FP['SOT-223'] = fp
bb = fp_bbox(fp, S)
text(S, sot223_c, bb[3] + 1.35, 'SOT-223', LBL, anchor='middle')
fp = place_lib(*PWR[1][1:3], dpak_c, 0, 90, 'U')
bb = fp_bbox(fp, S); fp.SetPosition(V(W - dpak_c, 37.3 - bb[1])); IC_FP['DPAK'] = fp
bb = fp_bbox(fp, S)
text(S, dpak_c, bb[3] + 1.35, 'DPAK', LBL, anchor='middle')
# refresh pad boxes of the two moved parts
BOXES[:] = [b for b in BOXES if b[6] not in (IC_FP['SOT-223'].GetReference(), IC_FP['DPAK'].GetReference())]
for k in ('SOT-223', 'DPAK'):
    for q in pad_boxes(IC_FP[k], S):
        BOXES.append((S, 'cu', q[0], q[1], q[2], q[3], IC_FP[k].GetReference()))

# --- pin-1 dots (silk, r 0.3) next to pin 1, pushed out until 0.3 mm clear of every pad
def pin1_dot(fp, side, direction, start=None, r=0.3, clear=0.3):
    pads = pad_boxes(fp, side)
    p1 = [q for q in pads if q[4] == '1'][0]
    dx, dy = direction
    cx = (p1[0] if dx < 0 else p1[2] if dx > 0 else (p1[0] + p1[2]) / 2)
    cy = (p1[1] if dy < 0 else p1[3] if dy > 0 else (p1[1] + p1[3]) / 2)
    if start:
        cx, cy = start
    def ok(cx, cy):
        for q in pads:
            ddx = max(q[0] - cx, 0, cx - q[2]); ddy = max(q[1] - cy, 0, cy - q[3])
            if math.hypot(ddx, ddy) < r + clear:
                return False
        return True
    n = math.hypot(dx, dy); ux, uy = dx / n, dy / n
    for _ in range(200):
        if ok(cx, cy):
            break
        cx += ux * 0.05; cy += uy * 0.05
    circle(side, 'silk', cx, cy, r, tag='dot')

for n in ('SOT-23', 'SOT-23-6', 'SOIC-8', 'TSSOP-14'):
    pin1_dot(IC_FP[n], S, (-1, -1))
pin1_dot(IC_FP['QFN-16'], S, (-1, -1))
pin1_dot(IC_FP['SOT-223'], S, (-1, 0))
pin1_dot(IC_FP['DPAK'], S, (-1, 0))
pin1_dot(MCU_FP['MSOP-8'], S, (-1, -1))
pin1_dot(MCU_FP['SOT-89'], S, (-1, 0))
pin1_dot(MCU_FP['SOT-363'], S, (-1, -1))
pin1_dot(MCU_FP['DFN-8'], S, (-1, -1))
for n in ('LQFP-48', 'QFN-32'):
    fp = MCU_FP[n]; bb = fp_bbox(fp, S)
    pin1_dot(fp, S, (-1, -1), start=(bb[0] + 0.55, bb[1] + 0.55))

# --- inch ruler (copper), R16 inruler(53.6)
def inruler(side, y, x0=4.0):
    rect(side, 'gold', x0 - 0.1, y - 0.125, 76.2 + 0.2, 0.25, tag='ruler')
    for i in range(31):
        xx = x0 + i * 2.54; Lr = 2.0 if i % 10 == 0 else (1.3 if i % 5 == 0 else 0.7)
        rect(side, 'gold', xx - 0.1, y - Lr, 0.2, Lr, tag='ruler')
        if i % 10 == 0 and i:
            text(side, xx, y - 2.5, str(i // 10), LBL, anchor='middle')
    text(side, x0 + 76.2 + 2.5, y - 1.8, 'in', LBL, anchor='middle')
inruler(S, 53.6)

# ================================================================ FRONT ===
S = 'F'
NAME_ST = dict(size=4.0, bold=True, wf=1.0)      # R16 5.6 px DejaVu Bold -> cap 4.08
TITLE_ST = dict(size=1.93, bold=False, wf=1.0)   # R16 2.7
MAIL_ST = dict(size=1.5, bold=False, wf=1.0)     # R16 2.1
text(S, 6, 12, 'Mauricio Mendes', NAME_ST)
text(S, 6, 18, 'Embedded & hardware engineer', TITLE_ST)
text(S, 6, 27.5, 'mauricio@hardcode.tools', MAIL_ST)
text(S, 6, 31.1, 'github.com/MauMendes', MAIL_ST)

# QR code: white silk = light modules + 0.9 mm quiet frame (dark modules = black mask)
QR_URL = 'https://maumendes.github.io'
q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, border=0)
q.add_data(QR_URL); q.make(fit=True)
QM = [[bool(v) for v in row] for row in q.get_matrix()]
QX, QY, QS, QP, QR_RX = 64.0, 9.5, 15.0, 0.9, 0.8
nq = len(QM); m = QS / nq
def arc_pts(cx, cy, r, a0, a1, n=8):
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)), cy + r * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]
X0, Y0, X1, Y1 = QX - QP, QY - QP, QX + QS + QP, QY + QS + QP
top = arc_pts(X0 + QR_RX, Y0 + QR_RX, QR_RX, 180, 270) + arc_pts(X1 - QR_RX, Y0 + QR_RX, QR_RX, 270, 360) + [(X1, QY), (X0, QY)]
bot = arc_pts(X1 - QR_RX, Y1 - QR_RX, QR_RX, 0, 90) + arc_pts(X0 + QR_RX, Y1 - QR_RX, QR_RX, 90, 180) + [(X0, QY + QS), (X1, QY + QS)]
poly(S, 'silk', top); poly(S, 'silk', bot)
rect(S, 'silk', X0, QY, QP, QS, box=False); rect(S, 'silk', QX + QS, QY, QP, QS, box=False)
for r_, row in enumerate(QM):
    c = 0
    while c < nq:
        if not row[c]:
            c0 = c
            while c < nq and not row[c]:
                c += 1
            rect(S, 'silk', QX + c0 * m, QY + r_ * m, (c - c0) * m, m, box=False)
        else:
            c += 1
BOXES.append(('F', 'silk', X0, Y0, X1, Y1, 'QR'))

# front mm ruler (copper) with silk numbers, R16 mmruler(52, up=True)
def mmruler(side, y, x0=4, x1=81):
    rect(side, 'gold', x0 - 0.1, y - 0.125, (x1 - x0) + 0.2, 0.25, tag='ruler')
    for i in range(int(x1 - x0) + 1):
        xx = x0 + i; Lr = 2.0 if i % 10 == 0 else (1.3 if i % 5 == 0 else 0.7)
        rect(side, 'gold', xx - 0.1, y - Lr, 0.2, Lr, tag='ruler')
        if i % 10 == 0 and i:
            text(side, xx, y - 2.6, str(i // 10), LBL, anchor='middle')
mmruler(S, 52)
text(S, 81.5, 50.2, 'cm', LBL, anchor='middle')

# ============================================================ groups/save ==
for gname, items_ in GROUPS.items():
    g = pcbnew.PCB_GROUP(board); g.SetName(gname); board.Add(g)
    for it in items_:
        g.AddItem(it)

board.Save(PCB)
CU_UUID = {sh.m_Uuid.AsString(): t for sh, t in CU_TAGS}

# ---- post-process: TTF texts, stackup (black mask, white silk, ENIG, 0.8 mm), title block
s = open(PCB).read()
STACKUP = '''		(stackup
			(layer "F.SilkS" (type "Top Silk Screen") (color "White"))
			(layer "F.Paste" (type "Top Solder Paste"))
			(layer "F.Mask" (type "Top Solder Mask") (color "Black") (thickness 0.01))
			(layer "F.Cu" (type "copper") (thickness 0.035))
			(layer "dielectric 1" (type "core") (color "FR4 natural") (thickness 0.71) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
			(layer "B.Cu" (type "copper") (thickness 0.035))
			(layer "B.Mask" (type "Bottom Solder Mask") (color "Black") (thickness 0.01))
			(layer "B.Paste" (type "Bottom Solder Paste"))
			(layer "B.SilkS" (type "Bottom Silk Screen") (color "White"))
			(copper_finish "ENIG")
			(dielectric_constraints no)
		)
'''
s = re.sub(r'\n\t\t\(model .*?\n\t\t\)', '', s, flags=re.S)   # no 3D bodies: nothing is assembled
# strip the library footprints' own silk / fab / courtyard graphics and texts (pads are kept)
s = re.sub(r'\n\t\t\((fp_line|fp_arc|fp_circle|fp_rect|fp_poly|fp_text|fp_curve)\b.*?\n\t\t\)', '', s, flags=re.S)
# decorative card, nothing is soldered: drop solder paste (no stencil) and paste-only sub-pads
s = re.sub(r'\n\t\t\(pad "" smd \w+\n(?:\t\t\t.*\n)*?\t\t\t\(layers "[FB]\.Paste"\)\n(?:\t\t\t.*\n)*?\t\t\)', '', s)
s = s.replace(' "B.Paste")', ')').replace(' "F.Paste")', ')')
s = s.replace('\t(setup\n', '\t(setup\n' + STACKUP, 1)
s = re.sub(r'\(title_block.*?\n\t\)\n', '', s, flags=re.S)
s = s.replace('\t(setup\n', '\t(title_block\n\t\t(title "PCB business card R16")\n\t\t(date "2026-10-28")\n'
              '\t\t(rev "R16")\n\t\t(company "Mauricio Mendes")\n'
              '\t\t(comment 1 "85 x 55 mm, 2 layers, 0.8 mm, black mask, white silk, ENIG. Decorative pads, no assembly.")\n\t)\n\t(setup\n', 1)
i = s.rstrip().rfind(')')
s = s[:i] + ''.join(TEXTS) + s[i:]
j = s.rfind('(embedded_fonts no)')
s = s[:j] + '(embedded_fonts yes)' + s[j + len('(embedded_fonts no)'):]
open(PCB, 'w').write(s)

# reload + save through KiCad: adds TTF render caches and embeds the two DejaVu fonts,
# so the board looks identical on a PC without DejaVu installed
b2 = pcbnew.LoadBoard(PCB)
b2.EmbedFonts()
b2.Save(PCB)

# ---- layout self-check (bounding boxes, view coordinates)
def gap(a, b):
    dx = max(b[2] - a[4], a[2] - b[4], 0); dy = max(b[3] - a[5], a[3] - b[5], 0)
    return math.hypot(dx, dy)
problems = []
for i, a in enumerate(BOXES):
    for b in BOXES[i + 1:]:
        if a[0] != b[0] or a[6] == b[6]:
            continue
        g = gap(a, b)
        deco = {'dot', 'band'} & {a[6], b[6]}
        if a[1] == 'silk' and b[1] == 'silk':
            if g < 0.2 and not deco:
                problems.append(('silk-silk', round(g, 3), a, b))
        elif a[1] != b[1]:
            if g < (0.15 if deco else 0.2):
                problems.append(('silk-cu', round(g, 3), a, b))
        elif g < 0.2:
            problems.append(('cu-cu', round(g, 3), a, b))
print(f'{len(problems)} layout proximity notes')
for p in problems:
    print(' ', p[0], p[1], p[2][0], p[2][6], [round(v, 2) for v in p[2][2:6]], '|', p[3][6], [round(v, 2) for v in p[3][2:6]])
json.dump({str(k): v for k, v in AWG_DRILL.items()}, open(os.path.join(HERE, 'awg_drills.json'), 'w'), indent=1)
print('AWG (wire, drill, pad):', AWG_DRILL)
print('wrote', PCB)

# ---- project design rules (JLCPCB 2-layer capabilities) + custom rule for the 0.1 mm demo
PRO = os.path.join(HERE, NAME + '.kicad_pro')
p = json.load(open(PRO))
d = p['board']['design_settings']
d['rules'].update({
    'min_clearance': 0.1,               # JLC absolute min spacing; only the demo pair uses it
    'min_copper_edge_clearance': 0.3,
    'min_hole_clearance': 0.25,
    'min_hole_to_hole': 0.25,
    'min_through_hole_diameter': 0.3,   # JLC min drill
    'min_track_width': 0.1,
    'min_connection': 0.1,
    'min_text_height': 1.0,
    'min_text_thickness': 0.15,
    'min_silk_clearance': 0.1,
    'min_via_annular_width': 0.13,
    'solder_mask_to_copper_clearance': 0.0,
})
d['rule_severities'].update({
    'silk_over_copper': 'error', 'silk_overlap': 'error', 'silk_edge_clearance': 'error',
    'text_height': 'error', 'text_thickness': 'error',
    # library footprints are used on purpose with their silk/fab/courtyard stripped
    'lib_footprint_mismatch': 'ignore',
})
nc = p['net_settings']['classes'][0]
nc['clearance'] = 0.15; nc['track_width'] = 0.15
p['meta']['filename'] = NAME + '.kicad_pro'
json.dump(p, open(PRO, 'w'), indent=2)
open(os.path.join(HERE, NAME + '.kicad_dru'), 'w').write('''(version 1)

# Intentional: the "<=30 V / 0.1" clearance demo pair on the back is drawn at
# JLCPCB's minimum spacing (0.1 mm). Everything else keeps >= 0.15 mm (netclass).
(rule "clearance_demo_0p1_JLC_min"
\t(condition "A.memberOfGroup('CLR_DEMO_0p1') && B.memberOfGroup('CLR_DEMO_0p1')")
\t(constraint clearance (min 0.1mm)))
''')
print('rules written')

# ---- DRC, then record the TrueType "stroke weight" findings as explicit exclusions.
# KiCad's TTF check deflates each glyph by min/2 and flags any glyph whose outline
# topology changes; DejaVu (like every TTF tested) has narrow joins where an arch
# meets a stem (n, m, b, d, 3, 6 ...). The actual stems are >= 0.2 mm (labels:
# DejaVu Sans Bold 1.0 x 0.8 -> 0.21 mm; front text >= 0.2 mm), so these are excluded.
def drc():
    out = os.path.join(HERE, 'drc.json')
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--severity-all', '--format', 'json', '-o', out, PCB], capture_output=True)
    return json.load(open(out))
r = drc()
excl = []
for v in r['violations']:
    if v['type'] == 'text_thickness':
        it = v['items'][0]
        key = '|'.join([v['type'], str(round(it['pos']['x'] * 1e6)), str(round(it['pos']['y'] * 1e6)), it['uuid'],
                        '00000000-0000-0000-0000-000000000000'])
        excl.append(key)
p = json.load(open(PRO))
p['board']['design_settings']['drc_exclusions'] = [[k, 'TTF glyph joins (DejaVu); real stems >= 0.2 mm'] for k in excl]
json.dump(p, open(PRO, 'w'), indent=2)
r = drc()
from collections import Counter
print('DRC after exclusions (not excluded):', Counter((v['severity'], v['type']) for v in r['violations'] if not v.get('excluded')), 'excluded:', sum(1 for v in r['violations'] if v.get('excluded')), 'unconnected', len(r.get('unconnected_items', [])))

# ---- copper-spacing verification on a throw-away copy.
# KiCad skips clearance checks between two items that both have no net, so on the
# real (netless) board DRC cannot see pad-to-pad spacing. Here every pad and every
# copper shape gets its own net (ruler pieces share one net per side, they overlap
# on purpose) and DRC is run with the same rules. The delivered board stays netless.
VDIR = os.path.join(HERE, 'verify'); os.makedirs(VDIR, exist_ok=True)
VPCB = os.path.join(VDIR, 'verify_nets.kicad_pcb')
vb = pcbnew.LoadBoard(PCB)
nid = [0]
def newnet(name=None):
    nid[0] += 1
    ni = pcbnew.NETINFO_ITEM(vb, name or f'V{nid[0]}'); vb.Add(ni); return ni
for fp in vb.GetFootprints():
    for pd in fp.Pads():
        if pd.IsOnCopperLayer():
            pd.SetNet(newnet())
ruler = {}
for d_ in vb.GetDrawings():
    if isinstance(d_, pcbnew.PCB_SHAPE) and d_.IsOnCopperLayer():
        tag = CU_UUID.get(d_.m_Uuid.AsString(), 'cu')
        if tag == 'ruler':
            key = d_.GetLayer()
            if key not in ruler:
                ruler[key] = newnet(f'RULER_{key}')
            d_.SetNet(ruler[key])
        else:
            d_.SetNet(newnet())
vb.Save(VPCB)
shutil.copy(PRO, os.path.join(VDIR, 'verify_nets.kicad_pro'))
shutil.copy(os.path.join(HERE, NAME + '.kicad_dru'), os.path.join(VDIR, 'verify_nets.kicad_dru'))
shutil.copy(os.path.join(HERE, 'fp-lib-table'), VDIR)
shutil.rmtree(os.path.join(VDIR, 'oss_card.pretty'), ignore_errors=True); shutil.copytree(HOLE_LIB_DIR, os.path.join(VDIR, 'oss_card.pretty'))
vout = os.path.join(VDIR, 'verify_drc.json')
subprocess.run(['kicad-cli', 'pcb', 'drc', '--severity-all', '--format', 'json', '-o', vout, VPCB], capture_output=True)
vr = json.load(open(vout))
vv = [v for v in vr['violations'] if v['type'] != 'text_thickness']
print('copper-spacing verification:', Counter((v['severity'], v['type']) for v in vv), 'unconnected', len(vr.get('unconnected_items', [])))
for v in vv[:40]:
    print('  ', v['type'], v['description'], '|', ' / '.join(i['description'] for i in v['items']), [ (i['pos']['x'], i['pos']['y']) for i in v['items']][:1])
