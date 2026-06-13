# -*- mode: python ; coding: utf-8 -*-
# onedir 번들: periscribe(컬렉터) + periscribe-proxy(서버) + periscribe-agent(런처)를
# 한 폴더(dist/Periscribe)에 파이썬 런타임 공유로 배치. 실행 시 임시추출(_MEI) 없음.
# 설치 프로그램(periscribe.iss)이 이 폴더를 %LOCALAPPDATA%\Programs\Periscribe 에 설치한다.
import os
from PyInstaller.utils.hooks import collect_all

HERE = os.path.dirname(os.path.abspath(SPEC))
COLLECTOR = os.path.abspath(os.path.join(HERE, "..", "collector"))

# 컬렉터(GUI+트레이)용 패키지 수집. 프록시는 customtkinter, 둘 다 cryptography 는 import 분석으로 자동 포함.
ck = collect_all("customtkinter")
ps = collect_all("pystray")
pil = collect_all("PIL")

a_col = Analysis(
    [os.path.join(HERE, "run_periscribe.py")],
    pathex=[COLLECTOR],
    binaries=ck[1] + ps[1] + pil[1],
    datas=ck[0] + ps[0] + pil[0],
    hiddenimports=ck[2] + ps[2] + pil[2] + ["pystray._win32", "PIL._tkinter_finder"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
a_proxy = Analysis(
    [os.path.join(HERE, "run_proxyserver.py")],
    pathex=[COLLECTOR],
    binaries=ck[1], datas=ck[0], hiddenimports=ck[2],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)
a_agent = Analysis(
    [os.path.join(HERE, "run_agent.py")],
    pathex=[COLLECTOR],
    binaries=[], datas=[], hiddenimports=[],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False, optimize=0,
)

pyz_col = PYZ(a_col.pure)
pyz_proxy = PYZ(a_proxy.pure)
pyz_agent = PYZ(a_agent.pure)

# windowed(콘솔 없음): 컬렉터·프록시. 콘솔: 에이전트(대화형 docker run -it 필요).
exe_col = EXE(pyz_col, a_col.scripts, [], exclude_binaries=True, name="periscribe",
              console=False, disable_windowed_traceback=False)
exe_proxy = EXE(pyz_proxy, a_proxy.scripts, [], exclude_binaries=True, name="periscribe-proxy",
                console=False, disable_windowed_traceback=False)
exe_agent = EXE(pyz_agent, a_agent.scripts, [], exclude_binaries=True, name="periscribe-agent",
                console=True, disable_windowed_traceback=False)

# 하나의 COLLECT 로 3 exe + 의존성을 한 폴더에. 같은 목적지 이름의 중복 파일은 PyInstaller 가 dedup
# (python312.dll 등 공유) → 런타임 3배 중복 방지.
coll = COLLECT(
    exe_col, a_col.binaries, a_col.datas,
    exe_proxy, a_proxy.binaries, a_proxy.datas,
    exe_agent, a_agent.binaries, a_agent.datas,
    strip=False, upx=False, name="Periscribe",
)
