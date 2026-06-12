"""PyInstaller 엔트리(periscribe-proxy.exe) — Claude API 프록시 '서버' 본체(독립 실행).
더블클릭/CLI 로 사용자가 직접 띄운다. 머신의 라우팅(on/off)은 컬렉터(periscribe.exe)가 한다."""
import sys

from periscribe.proxyserver import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
