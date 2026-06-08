"""PyInstaller 엔트리 스크립트(console exe). periscribe-agent 런처를 호출한다.

periscribe.exe(컬렉터, windowed)와 별개의 실행 파일 periscribe-agent.exe(console)로 빌드된다.
"""
import sys

from periscribe.agent import main_agent

if __name__ == "__main__":
    sys.exit(main_agent())
