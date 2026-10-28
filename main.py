"""Generate an engineering battery test PDF from timestamped CSV measurements."""
import argparse
from pathlib import Path
from report import build_report

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('csv_file',type=Path)
    parser.add_argument('--output','-o',type=Path,default=Path('engineering_report.pdf'))
    parser.add_argument('--max-voltage',type=float,help='Maximum allowed voltage (V)')
    parser.add_argument('--max-abs-current',type=float,help='Maximum allowed absolute current (A)')
    args=parser.parse_args()
    try:
        metrics=build_report(
            args.csv_file,args.output,args.csv_file.name,args.max_voltage,args.max_abs_current
        )
    except (ValueError, OSError) as exc:
        parser.exit(1,f'Error: {exc}\n')
    print(f'Created: {args.output}')
    for key,value in metrics.items(): print(f'{key}: {value}')
if __name__ == '__main__': main()
