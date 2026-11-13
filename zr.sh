#!/bin/bash
# zr.sh <tab> [bytes] — 只读串口尾窗
curl -s "http://127.0.0.1:8300/api/tabs/$1/receive?tail=1&max_bytes=${2:-4000}" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d["data"]["text"] if "data" in d and "text" in d["data"] else d)'
