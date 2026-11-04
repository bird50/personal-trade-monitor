"""Read the local paper account. --watch refreshes every five seconds."""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from app.config import Settings


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    config=Settings.from_env()
    with httpx.Client(base_url='http://127.0.0.1:8002',headers={'Authorization':'Bearer '+config.api_token},trust_env=False,timeout=5) as client:
        while True:
            try:
                account=client.get('/paper/account');account.raise_for_status()
                positions=client.get('/paper/positions');positions.raise_for_status()
                print(json.dumps({'account':account.json(),'open_positions':positions.json()['items']},indent=2),flush=True)
            except httpx.HTTPError:
                raise SystemExit('Paper service unavailable or unauthorized. Check scripts/run_xm.sh.') from None
            if not args.watch:
                break
            time.sleep(5)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
