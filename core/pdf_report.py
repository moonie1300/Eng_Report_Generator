"""Create a PDF report from validated battery measurements."""
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.graphics.shapes import Drawing, PolyLine, Line, String


def chart(rows, index, title, transform=lambda x: x):
    drawing = Drawing(460, 125)
    values = [transform(row[index]) for row in rows]
    low, high = min(values), max(values)
    padding = max((high-low)*0.15, 0.1)
    low, high = low-padding, high+padding
    start, duration = rows[0][0], (rows[-1][0]-rows[0][0]).total_seconds()
    points = []
    for row, value in zip(rows, values):
        points.extend((36+(row[0]-start).total_seconds()/duration*412, 19+(value-low)/(high-low)*80))
    drawing.add(String(36, 112, title, fontSize=10, fontName='Helvetica-Bold'))
    drawing.add(Line(36, 19, 448, 19, strokeColor=colors.grey))
    drawing.add(PolyLine(points, strokeColor=colors.HexColor('#18799c'), strokeWidth=1.8))
    drawing.add(String(0, 94, f'{high:.1f}', fontSize=8))
    drawing.add(String(0, 19, f'{low:.1f}', fontSize=8))
    drawing.add(String(36, 4, rows[0][0].strftime('%H:%M'), fontSize=8))
    drawing.add(String(417, 4, rows[-1][0].strftime('%H:%M'), fontSize=8))
    return drawing


def make_pdf(rows, metrics, output, input_name):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        leftMargin=20*mm,
        rightMargin=20*mm,
        topMargin=17*mm,
        bottomMargin=17*mm,
    )
    styles = getSampleStyleSheet()
    title = Paragraph('Engineering Battery Test Report', styles['Title'])
    intro = Paragraph(
        f'Source: {input_name} | '
        f'{rows[0][0].isoformat(sep=" ", timespec="minutes")} to '
        f'{rows[-1][0].isoformat(sep=" ", timespec="minutes")}',
        styles['Normal'],
    )
    table = Table([['Metric', 'Result']] + list(metrics.items()), colWidths=[83*mm, 86*mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#213d60')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f1f4f8')]),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    method = (
        'Method: P = V x I; energy is trapezoidal integrated power over time. '
        'Negative current denotes charging and positive current denotes discharging. '
        'Efficiency equals discharge energy divided by charge energy. For meaningful '
        'round-trip efficiency, use a comparable full cycle with matching starting/ending '
        'SOC. This is not a certified electrical safety report.'
    )
    story = [
        title,
        intro,
        Spacer(1, 8*mm),
        table,
        Spacer(1, 5*mm),
        chart(rows, 1, 'Voltage (V)'),
        chart(rows, 2, 'Current (A): negative = charging'),
        Spacer(1, 4*mm),
        Paragraph(method, styles['Normal']),
    ]
    doc.build(story)
