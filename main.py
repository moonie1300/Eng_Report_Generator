"""Generate an engineering battery test PDF from timestamped CSV measurements."""
import argparse
import csv
from datetime import datetime
from pathlib import Path
from statistics import mean
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.graphics.shapes import Drawing, PolyLine, Line, String

COLUMNS = ('timestamp', 'voltage_v', 'current_a', 'soc_pct')

def read_measurements(path):
    with open(path, newline='', encoding='utf-8-sig') as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames or not all(c in reader.fieldnames for c in COLUMNS):
            raise ValueError('CSV needs columns: ' + ', '.join(COLUMNS))
        rows = []
        for line, item in enumerate(reader, 2):
            try:
                time = datetime.fromisoformat(item['timestamp'].replace('Z', '+00:00'))
                voltage = float(item['voltage_v'])
                current = float(item['current_a'])
                soc = float(item['soc_pct'])
                if voltage <= 0 or not 0 <= soc <= 100:
                    raise ValueError('voltage must be >0 and SOC must be 0 to 100')
                rows.append((time, voltage, current, soc))
            except (ValueError, TypeError) as exc:
                raise ValueError(f'Invalid CSV line {line}: {exc}') from exc
    if len(rows) < 2 or any(b[0] <= a[0] for a, b in zip(rows, rows[1:])):
        raise ValueError('At least two rows with strictly increasing timestamps are required')
    return rows

def compute_metrics(rows):
    charge = discharge = 0.0
    for a, b in zip(rows, rows[1:]):
        hours = (b[0] - a[0]).total_seconds() / 3600
        p0, p1 = a[1]*a[2]/1000, b[1]*b[2]/1000
        segments = [(p0, p1, hours)]
        if p0 * p1 < 0:  # Split crossing so energy signs do not cancel.
            fraction = abs(p0)/(abs(p0)+abs(p1))
            segments = [(p0, 0, hours*fraction), (0, p1, hours*(1-fraction))]
        for start, end, length in segments:
            e = (start + end)/2 * length
            if e < 0: charge -= e
            else: discharge += e
    volts = [r[1] for r in rows]
    amps = [r[2] for r in rows]
    power = [v*i/1000 for _,v,i,_ in rows]
    return {
        'Duration (h)': f'{(rows[-1][0]-rows[0][0]).total_seconds()/3600:.2f}',
        'Samples': str(len(rows)),
        'Charge energy (kWh)': f'{charge:.2f}',
        'Discharge energy (kWh)': f'{discharge:.2f}',
        'Round-trip efficiency (%)': f'{100*discharge/charge:.2f}' if charge else 'N/A',
        'Voltage min / avg / max (V)': f'{min(volts):.2f} / {mean(volts):.2f} / {max(volts):.2f}',
        'Current min / max (A)': f'{min(amps):.2f} / {max(amps):.2f}',
        'Peak charge / discharge (kW)': f'{max(0,-min(power)):.2f} / {max(0,max(power)):.2f}',
        'SOC start / end (%)': f'{rows[0][3]:.1f} / {rows[-1][3]:.1f}',
    }

def chart(rows, index, title, transform=lambda x:x):
    drawing = Drawing(460,125)
    values = [transform(r[index]) for r in rows]
    low, high = min(values), max(values)
    padding = max((high-low)*0.15, 0.1)
    low, high = low-padding, high+padding
    start, duration = rows[0][0], (rows[-1][0]-rows[0][0]).total_seconds()
    points = []
    for row, value in zip(rows, values):
        points.extend((36+(row[0]-start).total_seconds()/duration*412, 19+(value-low)/(high-low)*80))
    drawing.add(String(36,112,title,fontSize=10,fontName='Helvetica-Bold'))
    drawing.add(Line(36,19,448,19,strokeColor=colors.grey))
    drawing.add(PolyLine(points,strokeColor=colors.HexColor('#18799c'),strokeWidth=1.8))
    drawing.add(String(0,94,f'{high:.1f}',fontSize=8))
    drawing.add(String(0,19,f'{low:.1f}',fontSize=8))
    drawing.add(String(36,4,rows[0][0].strftime('%H:%M'),fontSize=8))
    drawing.add(String(417,4,rows[-1][0].strftime('%H:%M'),fontSize=8))
    return drawing

def make_pdf(rows, metrics, output, input_name):
    output = Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    doc = SimpleDocTemplate(str(output),pagesize=A4,leftMargin=20*mm,rightMargin=20*mm,topMargin=17*mm,bottomMargin=17*mm)
    styles=getSampleStyleSheet()
    title=Paragraph('Engineering Battery Test Report', styles['Title'])
    intro=Paragraph(f'Source: {input_name} | {rows[0][0].isoformat(sep=" ",timespec="minutes")} to {rows[-1][0].isoformat(sep=" ",timespec="minutes")}',styles['Normal'])
    table=Table([['Metric','Result']]+list(metrics.items()),colWidths=[83*mm,86*mm])
    table.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#213d60')),
        ('TEXTCOLOR',(0,0),(-1,0),colors.white),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#f1f4f8')]),
        ('FONTSIZE',(0,0),(-1,-1),9),
        ('TOPPADDING',(0,0),(-1,-1),5),
        ('BOTTOMPADDING',(0,0),(-1,-1),5),
    ]))
    story=[title,intro,Spacer(1,8*mm),table,Spacer(1,5*mm),chart(rows,1,'Voltage (V)'),chart(rows,2,'Current (A): negative = charging'),Spacer(1,4*mm),Paragraph('Method: P = V x I; energy is trapezoidal integrated power over time. Negative current denotes charging and positive current denotes discharging. Efficiency equals discharge energy divided by charge energy. For meaningful round-trip efficiency, use a comparable full cycle with matching starting/ending SOC. This is not a certified electrical safety report.',styles['Normal'])]
    doc.build(story)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_file',type=Path)
    parser.add_argument('--output','-o',type=Path,default=Path('engineering_report.pdf'))
    args=parser.parse_args()
    try:
        rows=read_measurements(args.csv_file)
        metrics=compute_metrics(rows)
        make_pdf(rows,metrics,args.output,args.csv_file.name)
    except (ValueError,OSError) as exc:
        parser.exit(1,f'Error: {exc}\n')
    print(f'Created: {args.output}')
    for key,value in metrics.items(): print(f'{key}: {value}')
if __name__ == '__main__': main()
