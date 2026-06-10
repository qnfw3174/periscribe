"""PyInstaller 엔트리 스크립트(periscribe-proxy.exe, windowed).
인자 없이(더블클릭) 실행되면 프록시 ON/OFF GUI 토글을 띄우고,
인자가 있으면 기존 CLI(main)로 그대로 위임한다."""
import sys

from periscribe.__main__ import main

if __name__ == "__main__":
    argv = sys.argv[1:] or ["proxy-gui"]
    sys.exit(main(argv))
