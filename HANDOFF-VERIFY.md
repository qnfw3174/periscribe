# 제출 전 검증 작업 지시서 (Claude Code용)

> 이 파일은 Claude Code에게 그대로 붙여넣거나 `@HANDOFF-VERIFY.md` 로 참조시키기 위한 지시서입니다.
> 검증이 끝나면 이 파일은 저장소에서 지워도 됩니다.

---

## 컨텍스트

이 저장소는 오픈소스 개발자대회 출품 준비 중이며 **기능 동결 상태**다.
출품작 제출 마감은 2026-08-27 18:00, 2차 평가에서 **출품작 기능테스트(실제 구현 검증)** 와
**라이선스 검증(충돌·위반 여부)** 이 이뤄진다. 따라서 지금 필요한 것은 새 기능이 아니라
**"낯선 사람이 이 저장소를 받아 실제로 돌릴 수 있는가"** 다.

## 절대 규칙

1. **기능 추가 금지.** 새 명령, 새 옵션, 새 모듈, 새 설정 키를 만들지 않는다.
2. **리팩터링 금지.** 파일 분할·이름 변경·구조 개선을 하지 않는다. 동작이 같아지는 변경이라도 하지 않는다.
3. 수정은 아래 세 종류만 허용한다.
   - (A) 테스트를 깨뜨리는 **명백한 버그**
   - (B) **문서와 실제 동작의 불일치** (문서 쪽을 고치는 것이 기본)
   - (C) **인코딩·오탈자·라이선스 고지 누락**
4. 그 밖에 발견한 개선점은 **코드를 건드리지 말고** `VERIFY-REPORT.md` 의 "미조치 항목"에 기록만 한다.
5. 판단이 서지 않으면 고치지 말고 보고한다. **불확실하면 보고, 확실하면 수정.**

## 산출물

저장소 루트에 `VERIFY-REPORT.md` 를 만들고 아래 형식으로 남긴다.

```markdown
# 검증 리포트 (YYYY-MM-DD)
## 요약
- 테스트: N개 중 M개 통과
- 수정한 것: N건
- 미조치 항목: N건
## 1. 테스트 결과
## 2. 수정 내역 (항목별로 파일·이유·변경 요지)
## 3. 미조치 항목 (이유 포함 — 왜 지금 고치지 않는가)
## 4. 재현 절차 검증 결과
## 5. 라이선스 확인 결과
```

---

## 작업 목록

### 1. 테스트

```
cd collector && python -m pytest tests/ -v
```

- 전부 통과하는가? 실패하면 원인을 분석하고, **테스트가 틀렸는지 코드가 틀렸는지** 판별해 보고한다.
- 수정은 (A)에 해당할 때만. 테스트를 통과시키려고 단언을 약화시키지 말 것.
- 실행 시간이 비정상적으로 긴 테스트가 있으면 기록한다(심사위원도 돌린다).

### 2. 인코딩 점검 (우선순위 높음)

전 소스·문서에 대해 UTF-8 디코딩이 실패하는 바이트가 있는지 확인한다.

```python
# 예시 — 저장소 전체 스캔
import pathlib
for p in pathlib.Path('.').rglob('*'):
    if p.is_file() and p.suffix in {'.py','.md','.json','.ps1','.iss','.sql','.ts','.js','.html','.css'}:
        try:
            p.read_bytes().decode('utf-8')
        except UnicodeDecodeError as e:
            print(p, e)
```

- 깨진 파일이 있으면 해당 위치를 특정하고, **주변 문맥으로 원래 한글을 복원**한다.
- 특히 `collector/periscribe/__main__.py` 의 상단 독스트링과 주석을 확인할 것
  (`모든 설정은`, `계속 진행됩니다`, `자가치유` 근처가 의심 지점으로 보고됨).
- BOM(`\ufeff`)이 붙은 파일이 있으면 목록만 보고한다(제거 여부는 판단 후 보고).
- `.gitattributes` 가 줄바꿈·인코딩을 어떻게 다루는지 확인하고 실제 파일 상태와 맞는지 본다.

### 3. 소스 실행 재현 (심사위원 시나리오)

README와 `docs/DEPLOY.md` 의 절차만 보고 따라갔을 때 실제로 되는지 확인한다.
**문서에 없는 지식을 동원해서 성공시키지 말 것** — 막히는 지점이 곧 결함이다.

- 깨끗한 가상환경에서 `pip install -e collector[dev]` 가 되는가?
- `python -m periscribe --dry-run` 이 동작하는가? (StdoutSink 경로)
- 문서에 적힌 명령/경로/파일명이 실제와 일치하는가?
- 불일치는 **문서를 고치는 것을 기본**으로 한다(코드가 명백히 틀린 게 아니면).

### 4. ingest URL 분리 변경 검증 ⚠ 이번에 새로 바뀐 부분

`collector/periscribe/__main__.py` 에서 하드코딩된 Supabase URL을 제거하고
`_default_ingest_url()` (환경변수 → `dist.json`) 로 해석하도록 바꿨다. **테스트되지 않았다.**

확인할 것:

- `_dist_config()` 의 탐색 경로 3곳이 의도대로 동작하는가
  (exe 옆 / `%LOCALAPPDATA%\Periscribe` / `collector/dist.json`)
- `install()` 이 URL 없이 호출되면 `ValueError` 를 던지는가
- 그 `ValueError` 가 **세 진입점 모두에서 사람이 읽을 메시지로** 표시되는가
  - `main()` 의 `setup` 분기 → try/except 추가됨 (확인)
  - `gui_setup()` (customtkinter) → 기존 `except Exception as e` 가 받는지 확인
  - `_gui_setup_tk()` (tkinter 폴백) → 동일 확인
- `DEFAULT_INGEST_URL` 상수를 참조하는 다른 코드/테스트가 있는지 전수 검색
  (하위 호환용으로 남겨뒀지만 값이 빈 문자열일 수 있음)
- `packaging/build.ps1` 의 주입 블록이 PowerShell 문법상 유효한가
  (`ConvertTo-Json`, backtick 이스케이프, `Set-Content -Encoding UTF8`)
- **`packaging/*.iss` 3개가 onedir 폴더를 재귀 포함하는지 확인** — `dist.json` 이 설치본에
  실제로 들어가야 한다. 포함되지 않는 방식이면 그 사실만 보고할 것(수정은 판단 후).

### 5. 의존성 대조

- `collector/pyproject.toml` 의 의존성과 실제 `import` 문을 대조한다.
  - 선언됐는데 안 쓰는 것 / 쓰는데 선언 안 된 것 목록화
  - `packaging/build.ps1` 이 설치하는 것(`pyinstaller customtkinter cryptography pystray pillow`)과
    `pyproject.toml` 의 `optional-dependencies` 가 어긋나는지 확인
    (`pystray`, `pillow` 는 현재 pyproject에 없음 — 사실이면 보고)
- Python 3.8 지원을 선언했는데 실제로는 3.9+ 문법(`dict[str, int]`, `X | None` 런타임 평가 등)을
  쓰고 있지 않은지 확인한다. `from __future__ import annotations` 가 없는 파일이 있으면 지적.

### 6. 라이선스 확인

`THIRD-PARTY-LICENSES.md` 가 새로 작성됐다. 사실 검증만 한다.

- 실제 설치되는 패키지 라이선스를 `pip-licenses` 등으로 확인하고 문서와 대조
- 문서에 없는 전이 의존성이 있으면 추가
- `pystray` 가 LGPL-3.0 인지 확인(문서의 §4 판단 근거)
- `LICENSE` 파일의 저작권자·연도가 채워져 있는지 확인

### 7. 비밀·개인정보 스캔

- 소스·문서·커밋된 설정에 남은 토큰/키/개인 식별 정보가 있는지 전수 검색
  (`supabase.co`, `eyJ`, `sk-`, `ANTHROPIC_API_KEY`, 개인 이메일, 실제 머신 이름 등)
- `web/config.example.js`, `collector/config.example.json` 에 실제 값이 들어가 있지 않은지
- `.gitignore` 가 `config.json`·인증서·spool·`dist.json` 을 실제로 다 막고 있는지
  (`git status --ignored` 로 확인)
- **git 히스토리에 이미 커밋된 적이 있는지**도 확인한다(`git log -S` 로 검색). 있으면 보고만 할 것 —
  히스토리 재작성은 절대 하지 말 것.

### 8. 문서 링크 점검

최근 문서를 대폭 갱신했다. 깨진 참조를 찾는다.

- `README.md`, `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `docs/CONTAINERS.md`,
  `periscribe-spec.md`, `THIRD-PARTY-LICENSES.md` 의 상대 경로 링크가 전부 실재하는가
- 문서가 언급하는 파일·설정 키·CLI 명령이 실제로 존재하는가
  (예: `os_exec_enabled`, `api_proxy_bind`, `gate_tool_use`, `periscribe audit-setup`)
- `docs/` 의 `.html` 파일들이 `.md` 와 내용이 어긋나면 지적만 한다(정리는 사람이 판단)

---

## 마지막에 할 것

1. `VERIFY-REPORT.md` 작성
2. `git status` 와 `git diff --stat` 결과를 리포트 말미에 붙여 **무엇을 바꿨는지 한눈에** 보이게
3. 커밋하지 말 것 — 사람이 diff를 검토한 뒤 직접 커밋한다
