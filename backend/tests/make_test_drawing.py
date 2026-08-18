"""Build a synthetic assembly drawing carrying one of every defect we check for."""
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas

W, H = landscape(A3)


def balloon(c, x, y, text, r=11, lower=None):
    c.circle(x, y, r)
    if lower is None:
        c.setFont("Helvetica", 7)
        c.drawCentredString(x, y - 2.5, text)
    else:
        c.line(x - r, y, x + r, y)
        c.setFont("Helvetica", 6)
        c.drawCentredString(x, y + 2.0, text)
        c.drawCentredString(x, y - 6.5, lower)


def table(c, x, y, rows, widths, title=None):
    rh = 14
    total_w = sum(widths)
    if title:
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x, y + 6, title)
    top = y
    for i, row in enumerate(rows):
        ry = top - i * rh
        c.line(x, ry, x + total_w, ry)
        cx = x
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 7)
        for w, cell in zip(widths, row):
            c.drawString(cx + 3, ry - 10, str(cell))
            cx += w
    bottom = top - len(rows) * rh
    c.line(x, bottom, x + total_w, bottom)
    cx = x
    for w in list(widths) + [0]:
        c.line(cx, top, cx, bottom)
        cx += w


def sheet1(c):
    c.setFont("Helvetica", 8)
    c.drawString(60, 60, "DRAWING NO: 12345-001    REV: 2    SCALE: 1:2    SHEET: 1 OF 2")

    c.setFont("Helvetica-Bold", 9)
    c.drawString(300, 720, "VIEW A-A")
    c.drawString(120, 250, "DETAIL B")

    balloon(c, 150, 700, "10/2")
    balloon(c, 250, 700, "10/8")
    balloon(c, 150, 600, "11/2")
    balloon(c, 150, 300, "11/2")
    balloon(c, 350, 600, "12/4")
    balloon(c, 450, 600, "99/1")
    balloon(c, 350, 500, "14")
    balloon(c, 450, 500, "15", lower="3")
    balloon(c, 550, 500, "16", lower="REF")
    balloon(c, 250, 400, "17/1")

    rows = [
        ["ITEM", "PART NO", "DESCRIPTION", "QTY"],
        ["10", "100-2201", "PLATE", "10"],
        ["11", "100-2202", "BRACKET", "2"],
        ["12", "100-2203", "BOLT M8", "6"],
        ["13", "100-2204", "WASHER", "3"],
        ["14", "100-2205", "SEALANT", "A/R"],
        ["15", "100-2206", "PIN", "3"],
        ["16", "100-2207", "COVER", "1"],
        ["17", "100-2208", "NUT", "1"],
        ["17", "100-2208", "NUT", "1"],
        ["", "", "TOTAL", "30"],
    ]
    table(c, 700, 740, rows, [40, 90, 170, 40], "PARTS LIST")


def sheet2(c):
    c.setFont("Helvetica", 8)
    c.drawString(60, 60, "DRAWING NO: 12345-001    REV: 2    SCALE: 1:2    SHEET: 2 OF 2")
    c.setFont("Helvetica-Bold", 9)
    c.drawString(300, 720, "VIEW C-C")
    balloon(c, 300, 640, "10/2")

    rows = [
        ["ITEM", "PART NO", "QTY"],
        ["12", "100-2203", "4"],
        ["20", "100-2210", "1"],
    ]
    table(c, 700, 700, rows, [40, 90, 40], "BOM EXTRACT — ITEMS ON THIS SHEET")


c = canvas.Canvas("test_drawing.pdf", pagesize=(W, H))
c.setLineWidth(0.6)
sheet1(c)
c.showPage()
c.setLineWidth(0.6)
sheet2(c)
c.showPage()
c.save()
print("written")
