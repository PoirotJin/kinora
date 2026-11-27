"""Repair a bounded page of review summary metadata; no inference or motion reads."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from g2h.agent_learning import reconcile_reviews


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--after',default='',help='next_after returned by the previous page')
    parser.add_argument('--limit',type=int,default=100,help='1..500 existing summary rows; default 100')
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    if not args.root.is_dir():parser.error('root must be an existing farm directory')
    if not 1<=args.limit<=500:parser.error('limit must be 1..500')
    print(json.dumps(reconcile_reviews(args.root.resolve(),args.after,args.limit,args.dry_run),ensure_ascii=False))


if __name__=='__main__':main()
