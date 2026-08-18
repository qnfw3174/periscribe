# 검증 리포트 (2026-08-18)

`HANDOFF-VERIFY.md` 의 8개 작업 항목을 수행했다. 기능 추가·리팩터링은 하지 않았고,
수정은 (A) 명백한 버그 / (B) 문서-동작 불일치 / (C) 라이선스 고지 누락 세 종류만 했다.

## 요약

- **테스트: 71개 중 71개 통과** (검증 시작 시점 기준으로는 71개 중 65개 통과 + 6개 실패였고,
  그 이전에는 `pip install` 자체가 실패해 테스트를 아예 돌릴 수 없었다)
- **수정한 것: 10건** (설치 차단 1, 테스트 실패 유발 버그 1, 빌드 차단 1, 버전 불일치 1,
  문서-동작 불일치 2, 라이선스 고지 누락 1, 의존성 선언 누락 1, `.gitignore` 보강 1,
  PowerShell 스크립트 인코딩 1)
- **미조치 항목: 6건**
- **빌드 파이프라인: 실제로 돌려 확인함** — `build.ps1` 전 구간 통과, 인스톨러 3개 생성

> **추가 반영 (검증 이후, 저장소 소유자 승인)**: 최초 리포트는 수정 7건 / 미조치 9건이었다.
> 이후 소유자 판단으로 ① 미조치였던 의존성 선언(§2-(8))과 `.gitignore` 보강(§2-(9))을 적용하고,
> ② **Inno Setup 을 설치해 빌드를 실제로 돌렸다.** 그 과정에서 PowerShell 스크립트
> 인코딩 결함(§2-(10))을 새로 발견해 고쳤고, 미검증으로 남아 있던 두 항목
> (`.iss` 컴파일 / `dist.json` 설치본 포함)이 **실증으로 해소**됐다.

가장 중요한 결과 두 가지:

1. **심사위원이 README 대로 따라가면 첫 단계에서 막혔다.** `pip install -e collector[dev]` 가
   실패했고(초기 커밋부터 존재한 결함), 성공시켜도 프록시 테스트 6개가 깨졌다. 둘 다 고쳤고
   지금은 새 가상환경에서 문서 그대로 따라가면 끝까지 동작한다.
2. **인스톨러 3개가 작성자의 옛 PC 절대경로(`C:\_pj\_periscribe\...`)를 가리키고 있었다.**
   그 경로는 현재 존재하지 않으므로 누가 빌드해도 실패한다.

---

## 1. 테스트 결과

### 실행 환경

| 항목 | 값 |
|---|---|
| Python | 3.14.2 (win32) — 이 PC에 설치된 유일한 버전 |
| 가상환경 | 새로 만든 clean venv 2개(수정 전/후 각각) |
| 명령 | `pip install -e collector[dev]` → `cd collector && python -m pytest tests/` |

### 경과

| 단계 | 결과 |
|---|---|
| 최초 시도 | **수집 단계에서 중단** — `pip install` 실패로 `cryptography` 없음, 5개 모듈 ImportError |
| `pyproject.toml` 수정 후 | **6 failed, 65 passed** (33.38s) |
| `proxycert.py` 수정 후 | **71 passed** (7.57s) |
| clean venv 재현(최종) | **71 passed** (7.52s) |

`DeprecationWarning` 을 포함한 경고는 0건이다.

### 실패했던 6개와 원인 판별

```
FAILED tests/test_proxy_concurrency.py::test_32_concurrent_requests_all_succeed
FAILED tests/test_proxy_concurrency.py::test_half_open_connection_does_not_block
FAILED tests/test_proxy_concurrency.py::test_concurrent_streams_with_health_probes
FAILED tests/test_proxy_concurrency.py::test_aborted_stream_then_next_request_ok
FAILED tests/test_proxy_failsafe_e2e.py::test_enable_routes_when_server_healthy
FAILED tests/test_proxy_failsafe_e2e.py::test_enable_saves_and_disable_restores_orig
```

**판별: 코드가 틀렸다(테스트가 아니라).** 단언을 약화시키지 않고 코드를 고쳤다.

에러 메시지가 `TimeoutError: handshake operation timed out` 으로 보여 처음엔 타이밍 문제로
보였으나, 단일 테스트로 좁히니 실제 원인은 인증서였다.

```
SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED]
certificate verify failed: Missing Authority Key Identifier
```

`proxycert.py` 가 만드는 자체 CA와 리프 인증서에 RFC 5280 이 요구하는
**SubjectKeyIdentifier(CA) / AuthorityKeyIdentifier(리프)** 확장이 없었다.

원인을 한 번 더 좁혔다. 처음엔 OpenSSL 3.5 의 엄격화 때문으로 판단했으나 **사실이 아니었다** —
이 환경의 OpenSSL 은 3.0.18 이다. 실제 방아쇠는 **Python 3.14 가
`ssl.create_default_context()` 에서 `VERIFY_X509_STRICT` 를 기본으로 켜기 시작한 것**이다.

```
>>> ssl.OPENSSL_VERSION          'OpenSSL 3.0.18 30 Sep 2025'
>>> bool(ctx.verify_flags & ssl.VERIFY_X509_STRICT)    True
```

즉 Python 3.13 이하에서는 이 결함이 드러나지 않는다. 심사위원이 3.13 이하를 쓰면 원래도
통과했겠지만, 인증서가 RFC 5280 에 맞지 않았던 것은 사실이므로 코드를 고치는 쪽이 옳다.

### 실행 시간

비정상적으로 느린 테스트는 **없다**. 수정 전 33초였던 것은 실패한 핸드셰이크가 타임아웃까지
기다렸기 때문이고, 수정 후에는 전체 7.5초다. 가장 느린 테스트도 1.21초다.

---

## 2. 수정 내역

### (1) `collector/pyproject.toml` — `pip install` 자체가 실패하던 문제 **[A]**

```toml
-readme = "../README.md"
+# readme 는 지정하지 않는다: setuptools 가 프로젝트 루트(collector/) 밖의 파일을 거부하므로
+# "../README.md" 를 쓰면 `pip install -e collector` 자체가 실패한다. README 는 저장소 루트 참조.
```

**이유**: setuptools 가 프로젝트 루트 밖 파일 참조를 거부한다.

```
distutils.errors.DistutilsOptionError:
Cannot access 'H:\_pj\periscribe\collector\../README.md'
(or anything outside 'H:\_pj\periscribe\collector')
ERROR: Failed to build 'file:///H:/_pj/periscribe/collector'
```

`git log -S` 로 확인하니 **초기 커밋(946d15a)부터 있던 문제**다. 편집 설치를 한 번도
깨끗한 환경에서 해본 적이 없어 드러나지 않았던 것으로 보인다. 이 저장소는 PyPI에 배포하지
않으므로 `readme` 메타데이터를 잃는 실질적 손해는 없다. (파일을 `collector/` 안으로 옮기거나
복사하는 방법도 있으나 그건 구조 변경이라 하지 않았다.)

### (2) `collector/periscribe/proxycert.py` — 인증서에 필수 확장 누락 **[A]**

세 곳을 고쳤다.

| 위치 | 변경 |
|---|---|
| `_gen_ca()` | `SubjectKeyIdentifier.from_public_key(...)` 추가 |
| `_gen_leaf()` | `AuthorityKeyIdentifier.from_issuer_public_key(...)` + `SubjectKeyIdentifier` 추가 |
| `_leaf_valid()` | AKI 없는 구버전 리프를 재발급 대상으로 판정(기존 `host.docker.internal` SAN 검사와 같은 자리·같은 방식) |

**설계 원칙 보존 확인**: `ensure_certs()` 의 "CA 는 재사용하고 리프만 재발급한다"
(= 이미 신뢰 중인 `ca.pem` 을 깨지 않는다)는 그대로다. 별도 스크립트로 CA serial 이
유지되는지 확인했다.

**검증**: 신규 발급 경로를 실제 TLS 로 확인했다.

```
CA SKI == leaf AKI: True
127.0.0.1 -> 200
localhost -> 200
```

**한 번 틀렸다가 바로잡은 것**: 처음에 "AKI 를 발급자 공개키에서 유도하므로 SKI 없는
구버전 CA 와도 대조에 실패하지 않는다"는 주석을 달았는데, 실제로 구버전 CA를 만들어
돌려보니 **틀렸다**. CA 자체가 거부된다.

```
certificate verify failed: Missing Subject Key Identifier
```

주석을 사실에 맞게 고쳤다. 이 한계는 §3-(1)에 미조치로 남겼다.

### (3) `packaging/periscribe.iss`, `periscribe-proxy.iss`, `periscribe-agent.iss` — 하드코딩 절대경로 **[A]**

```
-#define DistDir "C:\_pj\_periscribe\packaging\dist\periscribe"
-#define PkgDir  "C:\_pj\_periscribe\packaging"
+; 경로는 이 .iss 파일 위치 기준(SourcePath)으로 잡는다 — 빌드하는 사람의 체크아웃 위치에 무관해야 한다.
+#define PkgDir  RemoveBackslash(SourcePath)
+#define DistDir PkgDir + "\dist\periscribe"
```

`OutputDir` 도 같은 절대경로였어서 `{#PkgDir}\dist` 로 바꿨다(3개 파일 모두).

**이유**: `C:\_pj` 는 이 PC에 **존재하지 않는다**(저장소는 `H:\_pj\periscribe`). 작성자의
옛 PC 경로가 남은 것으로, 누가 `build.ps1` 을 돌려도 ISCC 단계에서 실패한다.
`SourcePath` / `RemoveBackslash()` 는 Inno Setup 전처리기(ISPP)의 표준 기능이다.

`[Files]` 는 3개 모두 `recursesubdirs createallsubdirs` 라 `build.ps1` 이 써넣는
`dist.json` 이 설치본에 포함된다 — 지시서 §4의 확인 요청 사항이며, **포함된다**가 답이다.

> ✅ **컴파일 검증 완료**(추가 반영). Inno Setup 6.7.3 을 설치해 `build.ps1` 을 끝까지 돌렸다.
> 세 인스톨러 모두 `Successful compile`. 상세는 §4 "빌드 파이프라인 실증" 참고.

### (4) `packaging/*.iss` ×3 — 버전 불일치 **[B]**

`MyAppVersion "0.1.1"` → `"0.2.1"`. `pyproject.toml` 과 `periscribe/__init__.py` 는
0.2.1 인데 인스톨러만 0.1.1 이었다(이번 작업 트리에서 0.1.0→0.2.1 로 올릴 때 누락된 것으로 보인다).

### (5) `README.md` — 의존성 설치 단계 누락 **[B]**

```diff
+ pip install -e collector        # 의존성(cryptography) 설치. 테스트까지: -e collector[dev]
  cd collector
  python -m periscribe
```

**이유**: 문서대로 따라가면 `ModuleNotFoundError: No module named 'cryptography'` 로 막힌다.
실제로 이 검증의 첫 시도가 그렇게 실패했다.

### (6) `docs/DEPLOY.md` — 같은 누락 **[B]**

"소스 실행" 항목에 `pip install -e collector` 와 테스트 실행 명령을 추가했다.

### (7) `THIRD-PARTY-LICENSES.md` — 번들되는 전이 의존성 고지 누락 **[C]**

`§1.1` 을 새로 추가했다. 지시서의 "문서에 없는 전이 의존성이 있으면 추가"에 해당한다.

| 구성요소 | 끌어오는 패키지 | 라이선스 |
|---|---|---|
| cffi | cryptography | MIT-0 |
| pycparser | cffi | BSD-3-Clause |
| darkdetect | customtkinter | BSD-3-Clause |
| packaging | customtkinter | Apache-2.0 또는 BSD-2-Clause |
| six | pystray | MIT |

갱신 이력에도 한 줄 추가했다.

### (8) `collector/pyproject.toml` — `pystray`·`pillow` 선언 누락 **[B]** *(추가 반영)*

지시서의 지적이 **사실이었다**. `build.ps1` 은 설치하는데(`pyinstaller customtkinter
cryptography pystray pillow`) `optional-dependencies` 에는 `customtkinter` 만 있었다.

```toml
-gui = ["customtkinter>=5"]
+gui = ["customtkinter>=5", "pystray>=0.19", "pillow>=9"]
```

`build.ps1` 이 설치하는 목록과 일치시켰다. 두 import 는 `_make_tray()` 안에서 try/except 로
감싸져 있어 없어도 수집은 정상 동작하지만(트레이만 비활성화), 선언과 실제가 어긋나 있던 것은
사실이므로 맞췄다.

**검증**: 새 가상환경에서 `pip install -e collector[dev,gui]` → `customtkinter 6.0.0`,
`pystray 0.19.5`, `pillow 12.3.0` 정상 설치, 테스트 71개 통과.

### (9) `.gitignore` — 인증서·spool·루트 config 방어선 추가 **[B]** *(추가 반영)*

```gitignore
+/config.json          # 루트에서 실행했을 때 생기는 config (collector/config.json 은 이미 차단됨)
+*.pem
+*.key
+_apilog/
+_osexec/
```

인증서와 spool 은 원래 `%LOCALAPPDATA%\Periscribe` 와 `watch_dir` 아래 — **저장소 밖**에
생성되므로 실제 노출은 없었다. 실수로 복사해 오는 경우를 막는 방어선이다.

**검증**: `git ls-files | git check-ignore --stdin` 으로 **추적 중인 파일 중 새 규칙에 걸리는
것이 하나도 없음**을 확인했다(기존 추적 파일을 실수로 무시하게 만들지 않는다).

### (10) `.ps1` 5개 — UTF-8 BOM 누락으로 한글이 깨짐 **[C]** *(추가 반영)*

빌드를 **실제로 돌리다가 발견**했다. 코드 리뷰로는 안 보이는 종류의 결함이다.

`build.ps1` 실행 중 경고문이 이렇게 나왔다.

```
WARNING: ingest URL ???놁뒿?덈떎 - ?ㅼ튂 ???ъ슜?먯뿉寃??ㅻ쪟媛 ?쒖떆?⑸땲??
```

콘솔 코드페이지 문제로 보였지만 아니었다. **Windows PowerShell 5.1 은 `.ps1` 파일에 BOM 이
없으면 UTF-8 이 아니라 ANSI(한국어 Windows 에서 CP949)로 읽는다.** 즉 PowerShell 이 소스
바이트를 잘못 디코딩한 것이라 출력 리다이렉트로도 해결되지 않는다.

```
BOM: 3C 23 0A          (EF BB BF 가 아님 = BOM 없음)
PSVersion: 5.1.26100.8737
(Get-Content)[72]  ->   "  collector\dist.example.json ??dist.json ?쇰줈 蹂듭궗??梨꾩슦嫄곕굹,`n" +
UTF-8 강제 디코딩    ->   "  collector\dist.example.json 을 dist.json 으로 복사해 채우거나,`n" +
```

한글을 포함한 `.ps1` **5개 전부** 같은 상태였다. UTF-8 BOM 을 붙였다.

| 파일 | 한글 | 비고 |
|---|---|---|
| `packaging/build.ps1` | 248자 | 빌드 경고·오류 메시지 |
| `packaging/uninstall-cleanup.ps1` | 115자 | **인스톨러가 제거 시 실행** — 사용자에게 직접 보인다 |
| `deploy/windows/install-collector.ps1` | 93자 | |
| `deploy/windows/run-collector.ps1` | 51자 | |
| `deploy/windows/uninstall-collector.ps1` | 19자 | |

**검증**: BOM 추가 후 ① PowerShell 5.1 이 한글을 정상 디코딩하고 ② 5개 전부 구문 파서 통과
③ `build.ps1` 을 다시 돌려 경고문이 온전히 출력되는 것까지 확인했다.

```
WARNING: ingest URL 이 없습니다 - 설치 시 사용자에게 오류가 표시됩니다.
  collector\dist.example.json 을 dist.json 으로 복사해 채우거나,
  $env:PERISCRIBE_DEFAULT_INGEST_URL 를 설정한 뒤 다시 빌드하세요.
```

> BOM 은 PowerShell 7 과 Git 에서 무해하며, `.gitattributes` 는 `.ps1` 에 아무 규칙도 걸지
> 않으므로 영향이 없다. `.bat` 은 CP949 라 애초에 대상이 아니다(§4 인코딩 점검 참고).

---

## 3. 미조치 항목

### (1) SKI 없는 구버전 CA 는 자동 복구되지 않는다

§2-(2)에서 확인했듯, 이미 `ca.pem` 을 만들어 쓰던 사용자는 리프를 재발급해도
CA 자체가 엄격 검증에서 거부된다. 해결하려면 `%LOCALAPPDATA%\Periscribe\ca.pem`·`ca.key` 를
지워 CA를 재생성해야 하고, 그러면 **실행 중인 Claude 세션의 TLS 신뢰가 한 번 끊긴다**.

**왜 안 고쳤나**: 자동 재생성은 문서화된 설계 원칙("CA 는 한 번 만들면 재사용 —
무중단 ON/OFF", 커밋 a644937)을 정면으로 바꾸는 동작 변경이다. 규칙 5(불확실하면 보고)에
해당한다. 코드 주석에 재생성 방법을 적어뒀다.

**참고**: 실제 관찰 대상인 Claude Code(Node)는 엄격 검증을 기본으로 켜지 않으므로
현시점 실사용 영향은 낮을 가능성이 크다. 다만 확인하지는 못했다.

### (2) git 히스토리에 실제 Supabase 프로젝트 ID 가 남아 있다

```
264d9e2 컬렉터: 더블클릭 토큰 입력만으로 자동 설치(setup) + 내장 ingest URL ...
        → wgzsjdmohbawfcxiicqc.supabase.co
```

작업 트리에서는 이번에 제거됐지만 히스토리에는 남아 있다.

**왜 안 고쳤나**: 지시서가 "히스토리 재작성은 절대 하지 말 것"이라고 명시했다.

**위험도 평가**: 낮다. 프로젝트 URL 은 비밀이 아니며(웹 UI가 어차피 호출한다),
ingest 는 유효한 device_token 없이는 insert 할 수 없다. **JWT 계열 키(`eyJhbGciOi`)는
히스토리 전체에서 0건**으로, anon 키·service_role 키가 커밋된 적은 없음을 확인했다.
그래도 신경 쓰인다면 Supabase 프로젝트를 새로 파는 편이 히스토리 재작성보다 간단하다.

### (3) ~~미추적 파일 4개~~ — **해소됨**

최초 검증 시점에 `docs/ARCHITECTURE.md`(README 가 "여기서 시작"이라고 링크하는 핵심 문서),
`THIRD-PARTY-LICENSES.md`, `collector/dist.example.json`, `HANDOFF-VERIFY.md` 가 미추적이었다.
이 상태로 제출되면 README·DEPLOY 의 링크와 절차가 깨진다는 점을 지적했다.

**해소**: 추가 반영 때 4개 모두 커밋했다. 클론한 저장소에서 링크 실재를 재확인했다.
`HANDOFF-VERIFY.md` 는 지시서상 지워도 되는 파일이지만, 소유자 판단으로 남겼다.

### (4) `install()` 의 ValueError 메시지가 GUI 라벨에서 잘릴 수 있다

메시지가 5줄(불릿 포함)인데 `gui_setup()` / `_gui_setup_tk()` 는 한 줄짜리 status 라벨에
`f"오류: {e}"` 로 넣는다. 세 진입점 모두 예외를 **받기는 한다**(콘솔은 `main()` 의
`except ValueError`, GUI 2개는 기존 `except Exception as e`) — 지시서 §4의 확인 요청은
충족된다. 다만 표시 품질은 확인하지 못했다(GUI 실행 필요).

**왜 안 고쳤나**: 레이아웃 변경이고, 동작 결함이 아니다.

### (5) Python 3.8 실런타임 검증을 못 했다

`requires-python = ">=3.8"` 선언을 다음과 같이 확인했다.

- 모든 `collector/periscribe/*.py` 에 `from __future__ import annotations` 있음
  (예외: `__init__.py` — 어노테이션이 없어 불필요)
- 어노테이션 밖에서 런타임 평가되는 3.9+ 문법: **0건**
- 3.9~3.12 신규 API(`removeprefix`, `match`, `tomllib`, `pairwise`, `slots=True` 등): **0건**
- 전 파일 `ast.parse(feature_version=(3,8))` **통과**

**왜 안 고쳤나**: 고칠 게 없다. 다만 이 PC에 3.8 인터프리터가 없어 실제 실행은 못 했다.
참고로 최신 `cryptography` 는 3.8 을 더 이상 지원하지 않으므로, 3.8 사용자는
구버전 `cryptography` 로 떨어진다(`>=41` 이므로 해석은 된다).

### (6) `web/index.html` 에 작성자 GitHub 계정명이 있다

`https://github.com/qnfw3174/periscribe-dist/releases/...` 6곳. 배포물 다운로드 링크이므로
**의도된 공개 정보**로 보인다. 개인정보 스캔 결과로 보고만 한다.

---

## 4. 재현 절차 검증 결과

### 심사위원 시나리오 (문서 밖 지식 동원 없이)

| 단계 | 최초 | 수정 후 |
|---|---|---|
| `pip install -e collector[dev]` | ❌ `DistutilsOptionError` | ✅ |
| `cd collector && python -m pytest tests/` | ❌ 5 ImportError → 6 failed | ✅ **71 passed** |
| `python -m periscribe --dry-run` | ✅ | ✅ |
| `python -m periscribe run --dry-run` | ✅ | ✅ |

`--dry-run` 은 StdoutSink 경로로 정규화된 이벤트 JSON 을 그대로 출력한다(정상).
기본이 EOF부터라 신규 활동만 잡힌다.

### 문서-실제 일치 확인

- 문서가 언급하는 **설정 키 18개 전수 확인** — `os_exec_enabled`, `api_log_enabled`,
  `api_proxy_bind`, `gate_tool_use`, `tool_block_patterns`, `block_patterns`,
  `redact_patterns`, `inject_system`, `block_message`, `container_root`,
  `workspace_writable`, `writable_paths`, `readonly_paths`, `drop_all_capabilities`,
  `no_new_privileges`, `read_only_rootfs`, `machine_guid`, `dek_keys` → **전부 코드에 존재**
- CLI 서브커맨드 — 문서의 `run`/`setup`/`audit-setup`/`proxy on` 이 `main()` 의 분기와 일치
- 마크다운 **상대경로 링크 30개 전수 검사 → 깨진 링크 0건**
  (단 §3-(3)의 미추적 파일이 커밋된다는 전제)
- `docs/*.html` 의 낡은 내용 검사 — 제거된 `guardian`, `schtasks`, `onefile`,
  하드코딩 URL, 구버전 번호 참조 **0건**. `.md` 와 어긋나는 부분을 찾지 못했다

### ingest URL 분리 변경 검증 (지시서 §4 — "테스트되지 않았다"고 표시된 부분)

전용 스크립트로 **17개 케이스 전부 통과**했다.

| 확인 항목 | 결과 |
|---|---|
| 탐색 경로 3곳 (exe 옆 / `%LOCALAPPDATA%` / `collector/dist.json`) | ✅ |
| 우선순위: 환경변수 > `%LOCALAPPDATA%` > `collector/dist.json` | ✅ |
| URL 없이 `install()` → `ValueError` | ✅ |
| 메시지에 환경변수명·`dist.json` 안내 포함 | ✅ |
| 깨진 `dist.json` → 예외 없이 빈 문자열 폴백 | ✅ |
| BOM 붙은 `dist.json` (`utf-8-sig`) 처리 | ✅ |
| `install(url=...)` 명시 인자가 최우선 | ✅ |
| 해석된 URL 이 `config.json` 에 기록 | ✅ |
| `DEFAULT_INGEST_URL` 상수 잔존 참조 전수 검색 | 코드 참조 없음(정의부만) |
| `build.ps1` 주입 블록 PowerShell 문법 | ✅ 파서 검증 통과(오류 0) |
| `*.iss` 3개가 onedir 폴더 재귀 포함 → `dist.json` 설치됨 | ✅ (단 §2-(3)의 경로 버그 수정 후) |

사용자에게 표시되는 메시지:

```
ingest 엔드포인트가 설정되지 않았습니다.
배포자가 아래 중 하나를 지정해야 합니다:
  • 환경변수 PERISCRIBE_DEFAULT_INGEST_URL
  • dist.json 의 ingest_url (collector/dist.example.json 참고)
예: https://<project>.supabase.co/functions/v1/ingest
```

### 빌드 파이프라인 실증 *(추가 반영)*

Inno Setup 6.7.3 을 `winget` 으로 설치하고 `packaging/build.ps1` 을 실제로 돌렸다.
시스템 파이썬 오염을 피하려고 검증용 venv 를 PATH 앞에 두고 실행했다
(`build.ps1` 20번 줄이 주변 `python` 에 `pip install` 을 하기 때문이다).

| 단계 | 결과 |
|---|---|
| `winget install JRSoftware.InnoSetup` | ✅ 6.7.3, `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` |
| ISCC 경로가 `build.ps1` 탐색 목록과 일치하는가 | ✅ 첫 번째 후보에 정확히 설치됨 |
| PyInstaller onedir ×3 | ✅ `onedir x3 done` |
| Inno Setup 컴파일 ×3 | ✅ `Successful compile` ×3 |

산출물:

| 인스톨러 | 크기 | onedir |
|---|---|---|
| `periscribe-setup.exe` | 18.2 MB | 54.5 MB / 1,209 파일 |
| `periscribe-proxy-setup.exe` | 17.2 MB | 50.9 MB / 1,071 파일 |
| `periscribe-agent-setup.exe` | 8.7 MB | 19.9 MB / 58 파일 |

`periscribe-agent` 가 19.9 MB / 58 파일로 확연히 작은 것은 설계대로다 —
표준 라이브러리만 쓰고 컬렉터 코드를 import 하지 않는다.

**`dist.json` 이 설치본에 실제로 들어가는가** (지시서 §4의 핵심 확인 요청):
`PERISCRIBE_DEFAULT_INGEST_URL` 을 넣고 다시 빌드해 ISCC 로그에서 확증했다.

```
Compressing: ...\dist\periscribe\dist.json
Compressing: ...\dist\periscribe-proxy\dist.json
Compressing: ...\dist\periscribe-agent\dist.json
```

주입 블록도 정상 동작한다 — 세 onedir 폴더 모두
`{"ingest_url": "https://example.invalid/functions/v1/ingest"}` 가 기록됐고,
URL 없이 돌리면 빌드를 막지 않고 경고만 내는 것도 확인했다(설계대로).

> 빌드 산출물(`packaging/dist/`, `packaging/build/`, `packaging/*.spec`)은
> `.gitignore` 가 전부 막고 있어 저장소가 오염되지 않는 것도 확인했다.
>
> 검증하지 **않은** 것: 생성된 인스톨러를 실제로 **실행**해 설치해 보지는 않았다
> (이 PC에 Periscribe 를 설치하고 자동시작을 등록하게 되므로). 컴파일과 패키징까지가 범위다.

### 인코딩 점검 (지시서 §2 — 우선순위 높음)

전 소스·문서(`.py .md .json .ps1 .iss .sql .ts .js .html .css .bat .toml`)를 스캔했다.

- **UTF-8 디코딩 실패: `packaging/uninstall.bat` 1개뿐 → 결함 아님**
  `.gitattributes` 가 `*.bat -text` 로 선언하고 그 이유("배치 파일은 CRLF + OEM(CP949)
  인코딩이 필요")를 적어뒀다. CP949 로 디코딩하면 한글 162자가 온전하고 U+FFFD 는 0개다.
  **의도된 인코딩이며 실제 파일 상태와 `.gitattributes` 선언이 일치한다.**
- **BOM 붙은 파일: 0개**
- 지시서가 의심 지점으로 지목한 `collector/periscribe/__main__.py` 상단 독스트링과
  주석(`모든 설정은`, `계속 진행됩니다`, `자가치유` 근처) — **깨짐 없음**. 정상 UTF-8 이다.
- pytest 출력에서 한글이 깨져 보이는 것은 **콘솔 코드페이지 문제**이지 소스 손상이 아니다
  (`PYTHONIOENCODING=utf-8` 을 주면 정상 출력된다).

---

## 5. 라이선스 확인 결과

### 결론: **충돌·위반 없음.** 번들되는 구성요소는 전부 허용적 라이선스다.

`pip-licenses` 로 실제 설치 메타데이터를 뽑아 문서와 대조했다.

| 문서의 주장 | 실제 메타데이터 | 판정 |
|---|---|---|
| cryptography — Apache-2.0 **또는** BSD-3-Clause (듀얼) | `Apache-2.0 OR BSD-3-Clause` | ✅ 일치 |
| customtkinter — MIT | `MIT License` | ✅ 일치 |
| Pillow — MIT-CMU | `MIT-CMU` | ✅ 일치 |
| **pystray — LGPL-3.0** (§4 판단 근거) | `GNU Lesser General Public License v3 (LGPLv3)` | ✅ **사실 확인됨** |

### 추가한 것 — 번들되는 전이 의존성 5건

문서에 없었다. Windows 에서 **무조건**(플랫폼 마커 평가 후) 함께 설치되어 PyInstaller
번들에 들어간다. §2-(7)에서 `§1.1` 로 추가했다.

`cffi`(MIT-0) · `pycparser`(BSD-3-Clause) · `darkdetect`(BSD-3-Clause) ·
`packaging`(Apache-2.0 또는 BSD-2-Clause) · `six`(MIT) — **전부 허용적**.

> 정확성을 위해 짚어둔다: `cryptography` 의 메타데이터에는 `bcrypt`, `typing-extensions` 도
> 보이지만 이들은 **extra 조건부**라 기본 설치에 포함되지 않는다. 마커를 평가해 걸러냈다.

개발·테스트 전용(`pytest`, `pluggy`, `iniconfig`, `Pygments`, `colorama`)은 배포물에
포함되지 않으므로 고지 대상에서 제외했고, 그 사실을 문서에 명시했다.

### `LICENSE` 파일

```
MIT License
Copyright (c) 2026 Periscribe contributors
```

저작권자·연도 **모두 채워져 있다**. 플레이스홀더(`<year>`, `<copyright holders>`) 없음.

### 남은 판단 사항

`pystray` LGPL-3.0 에 대한 문서 §4의 대응(현행 유지 + 고지 + 소스 공개로 재링크 보장)은
합리적인 선택이다. 다만 이는 **법적 판단이므로 검증 범위 밖**이며, 대회 심사가 LGPL
번들에 엄격하다면 §4의 2안(트레이를 Tk 로 대체)을 미리 검토해둘 가치가 있다.

---

## 부록: `git status` / `git diff --stat`

> 최초 작성 시점에는 커밋하지 않은 상태였다. 이후 **저장소 소유자 승인으로 `main` 에 커밋**했고
> (§2-(8)·§2-(9) 추가 반영 포함), 그때의 상태를 아래 "커밋 결과"에 남긴다.

### 커밋 직전 상태 (참고)

```
$ git status --short
 M .gitignore
 M README.md
 M collector/periscribe/__init__.py
 M collector/periscribe/__main__.py
 M collector/periscribe/proxycert.py
 M collector/pyproject.toml
 M docs/CONTAINERS.md
 M docs/DEPLOY.md
 M packaging/build.ps1
 M packaging/periscribe-agent.iss
 M packaging/periscribe-proxy.iss
 M packaging/periscribe.iss
 M periscribe-spec.md
?? HANDOFF-VERIFY.md
?? THIRD-PARTY-LICENSES.md
?? collector/dist.example.json
?? docs/ARCHITECTURE.md
```

```
$ git diff --stat
 .gitignore                        |   5 ++
 README.md                         | 112 +++++++++++++++++++++++++++++---------
 collector/periscribe/__init__.py  |  19 +++++--
 collector/periscribe/__main__.py  |  64 +++++++++++++++++++---
 collector/periscribe/proxycert.py |  16 +++++-
 collector/pyproject.toml          |   5 +-
 docs/CONTAINERS.md                |  18 ++++++
 docs/DEPLOY.md                    |  74 ++++++++++++++++++++-----
 packaging/build.ps1               |  24 ++++++++
 packaging/periscribe-agent.iss    |   8 ++-
 packaging/periscribe-proxy.iss    |   8 ++-
 packaging/periscribe.iss          |   7 ++-
 periscribe-spec.md                |  28 +++++++++-
 13 files changed, 323 insertions(+), 65 deletions(-)
```

### 어느 변경이 누구 것인가 (중요)

커밋에는 **검증 전부터 있던 미커밋 변경과 이번 검증의 수정이 함께** 들어갔다.
이번 검증에서 건드린 파일은 다음 9개다.

| 파일 | 이번 검증의 변경 |
|---|---|
| `collector/pyproject.toml` | `readme` 줄 제거 + `gui` extra 에 `pystray`/`pillow` (**버전 0.1.0→0.2.1 은 기존 것**) |
| `collector/periscribe/proxycert.py` | **전부 이번 검증** — SKI/AKI 추가, `_leaf_valid` AKI 검사 |
| `packaging/periscribe.iss` | **전부 이번 검증** — 경로 상대화, 버전 |
| `packaging/periscribe-proxy.iss` | **전부 이번 검증** — 경로 상대화, 버전 |
| `packaging/periscribe-agent.iss` | **전부 이번 검증** — 경로 상대화, 버전 |
| `README.md` | `pip install -e collector` 한 줄 추가 (**나머지 111줄은 기존 것**) |
| `docs/DEPLOY.md` | "소스 실행" 항목 2줄 (**나머지는 기존 것**) |
| `.gitignore` | `*.pem`/`*.key`/`/config.json`/spool 추가 (**앞의 5줄은 기존 것**) |
| `THIRD-PARTY-LICENSES.md` | `§1.1` 전이 의존성 + 갱신 이력 (미추적 파일이라 `diff --stat` 에는 안 잡혔다) |

건드리지 않은 파일: `collector/periscribe/__init__.py`, `collector/periscribe/__main__.py`,
`docs/CONTAINERS.md`, `packaging/build.ps1`, `periscribe-spec.md`, `docs/ARCHITECTURE.md`,
`collector/dist.example.json`.

### 커밋 결과

`main` 에 2개 커밋으로 나눠 넣었다 — 기존 미커밋 작업과 이번 검증 수정을 가능한 만큼 분리했다.

1. **`기존: ingest URL 분리 + 문서 갱신`** — 검증 전부터 작업 트리에 있던 변경만.
   `__init__.py`, `__main__.py`, `docs/CONTAINERS.md`, `packaging/build.ps1`,
   `periscribe-spec.md`, `docs/ARCHITECTURE.md`, `collector/dist.example.json`
2. **`검증: 제출 전 재현성 수정`** — 이번 검증의 수정 전체 + 이 리포트.

> ⚠ **완전한 분리는 불가능했다.** `README.md`, `docs/DEPLOY.md`, `collector/pyproject.toml`,
> `.gitignore`, `THIRD-PARTY-LICENSES.md` 5개 파일은 기존 변경과 검증 수정이 **같은 파일
> (일부는 인접한 줄)에 섞여** 있어 hunk 단위로 가르면 오히려 위험했다. 이 5개는 2번 커밋에
> 함께 들어갔으므로, 2번 커밋에는 기존 문서 갱신분도 포함돼 있다. 줄 단위 귀속은 위의
> "어느 변경이 누구 것인가" 표를 기준으로 본다.

### 3번째 커밋 (빌드 실증)

Inno Setup 설치 후 빌드를 돌리는 과정에서 나온 변경이다.

- `.ps1` 5개에 UTF-8 BOM 추가 (§2-(10))
- 이 리포트 갱신 — §2-(3)·§4 의 "컴파일 미검증" 표기 해소, 미조치 7건 → 6건

### 남은 후속 작업 (사람이 판단)

1. §3-(1) 구버전 CA 재생성을 자동화할지 결정 — 실사용 영향은 낮을 가능성이 크다
2. §3-(2) 히스토리의 Supabase 프로젝트 ID — 신경 쓰이면 프로젝트를 새로 파는 편이 간단하다
3. 제출 직전 실제 배포용 빌드는 `PERISCRIBE_DEFAULT_INGEST_URL` 또는 `collector/dist.json` 에
   **진짜 엔드포인트**를 넣고 다시 돌려야 한다(이번 검증은 `example.invalid` 로만 확인했다)
