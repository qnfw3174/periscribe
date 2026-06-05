"""PyInstaller 엔트리 스크립트. periscribe 패키지의 CLI를 호출한다."""
import sys

from periscribe.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
