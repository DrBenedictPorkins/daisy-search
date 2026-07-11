#!/usr/bin/env bash
# Usage: LEAKCHECK_API_KEY=xxx ./scripts/leakcheck.sh email1 email2 ...
# Example: LEAKCHECK_API_KEY=xxx ./scripts/leakcheck.sh "<email>" "<phone>"

set -euo pipefail

if [[ -z "${LEAKCHECK_API_KEY:-}" ]]; then
  echo "Error: LEAKCHECK_API_KEY env var not set" >&2
  exit 1
fi

if [[ $# -eq 0 ]]; then
  echo "Usage: LEAKCHECK_API_KEY=xxx $0 <email|phone|username> ..." >&2
  exit 1
fi

for query in "$@"; do
  echo ""
  echo "=== $query ==="
  curl -s \
    -H "X-API-Key: ${LEAKCHECK_API_KEY}" \
    "https://leakcheck.io/api/v2/query/$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$query")" \
  | python3 -c "
import json, sys
d = json.load(sys.stdin)
if not d.get('success'):
    print('  Error:', d)
    sys.exit()
found = d.get('found', 0)
if found == 0:
    print('  Not found in any breach')
else:
    print(f'  Found in {found} records')
    results = d.get('result', [])
    for r in results:
        src = r.get('source', {})
        print(f'  [{src.get(\"name\",\"?\")}, {src.get(\"breach_date\",\"no date\")}]')
        for k, v in r.items():
            if k == 'source':
                continue
            if v:
                print(f'    {k}: {v}')
"
  sleep 1
done
