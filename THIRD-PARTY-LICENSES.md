# 서드파티 라이선스 고지 (THIRD-PARTY NOTICES)

Periscribe 본체는 **MIT License**로 배포됩니다([LICENSE](./LICENSE)).
이 문서는 Periscribe가 사용·번들·참조하는 서드파티 구성요소와 각각의 라이선스를 정리합니다.

> 각 라이선스의 전문은 해당 프로젝트 배포물에 포함되어 있습니다. 아래 표기는 고지 목적이며,
> 버전 갱신 시 이 문서도 함께 갱신해야 합니다.

---

## 1. 런타임 의존성 — 배포물에 포함됨

설치 프로그램(`periscribe-setup.exe` 등)에 실제로 번들되는 구성요소입니다.

| 구성요소 | 용도 | 라이선스 | MIT 배포와의 관계 |
|---|---|---|---|
| [CPython](https://www.python.org/) 3.8+ | 런타임 | PSF License 2.0 | 허용적. 고지로 충족 |
| [cryptography](https://github.com/pyca/cryptography) ≥41 | E2EE(AES-GCM, RSA-OAEP), 프록시 인증서 발급 | Apache-2.0 **또는** BSD-3-Clause (듀얼) | 허용적. Apache-2.0 선택 시 NOTICE 고지 필요 → 본 문서로 충족 |
| [OpenSSL](https://www.openssl.org/) | `cryptography`가 정적 링크 | Apache-2.0 (3.x) | 허용적. 고지로 충족 |
| [customtkinter](https://github.com/TomSchimansky/CustomTkinter) ≥5 | 설치 GUI, 프록시 상태창 | MIT | 무관 |
| [pystray](https://github.com/moses-palmer/pystray) | 트레이 컨트롤 패널 | LGPL-3.0 | **§4 참고** — 검토 필요 |
| [Pillow](https://github.com/python-pillow/Pillow) | 트레이 아이콘 이미지 | MIT-CMU | 허용적 |

### 1.1 전이 의존성 — 위 항목이 끌어오며 함께 번들됨

직접 import 하지는 않지만 Windows 설치 시 위 패키지들이 무조건 함께 설치되어
PyInstaller 번들에 포함됩니다. 모두 허용적 라이선스로 MIT 배포와 충돌하지 않습니다.

| 구성요소 | 끌어오는 패키지 | 라이선스 |
|---|---|---|
| [cffi](https://github.com/python-cffi/cffi) | cryptography | MIT-0 |
| [pycparser](https://github.com/eliben/pycparser) | cffi | BSD-3-Clause |
| [darkdetect](https://github.com/albertosottile/darkdetect) | customtkinter | BSD-3-Clause |
| [packaging](https://github.com/pypa/packaging) | customtkinter | Apache-2.0 **또는** BSD-2-Clause (듀얼) |
| [six](https://github.com/benjaminp/six) | pystray | MIT |

> 개발·테스트 전용(`pytest`, `pluggy`, `iniconfig`, `Pygments`, `colorama` 등)은
> 배포물에 포함되지 않으므로 고지 대상이 아닙니다.

## 2. 빌드 도구 — 배포물에 포함되지 않음

빌드 시점에만 사용되며 산출물에 코드가 들어가지 않거나, 예외 조항으로 자유 배포가 가능한 것들입니다.

| 구성요소 | 용도 | 라이선스 | 비고 |
|---|---|---|---|
| [PyInstaller](https://pyinstaller.org/) | onedir 실행 파일 번들 | GPL-2.0+ **with bootloader exception** | 부트로더 예외 조항에 따라 **번들 결과물은 원하는 라이선스로 배포 가능**. Periscribe는 부트로더를 수정하지 않음 |
| [Inno Setup](https://jrsoftware.org/isinfo.php) | Windows 설치 프로그램 생성 | 수정 BSD 스타일 (JRSoftware) | 생성된 설치 프로그램의 자유 배포 허용 |

## 3. 외부 프로그램 — 사용자가 직접 설치 (재배포하지 않음)

Periscribe는 아래를 **저장소나 설치 프로그램에 포함하지 않으며**, 사용자가 각 공급자로부터
직접 설치합니다. `periscribe audit-setup`은 공식 URL에서 사용자의 동의 하에 내려받습니다.

| 구성요소 | 용도 | 라이선스 | 취급 |
|---|---|---|---|
| [Sysmon](https://learn.microsoft.com/sysinternals/downloads/sysmon) (Sysinternals) | OS 프로세스 실행 감사 | Microsoft Sysinternals EULA — **재배포 불가** | 바이너리를 포함하지 않고 공식 다운로드 URL만 참조. 선택 기능 |
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) / [Podman](https://podman.io/) | 샌드박스 컨테이너 런타임 | Docker Subscription Service Agreement / Apache-2.0 | 사용자 설치. Docker Desktop은 대기업 유상 조건 있음 → Podman 폴백 지원 |
| [Claude Code](https://www.anthropic.com/claude-code) | 관찰 대상 | Anthropic 상용 약관 | 포함하지 않음. 관찰 대상일 뿐 |

## 4. 검토 필요 — pystray (LGPL-3.0)

`pystray`는 LGPL-3.0이며, MIT 프로젝트가 **정적 링크에 준하는 형태로 번들**할 때 주의가 필요합니다.
PyInstaller onedir 번들은 Python 바이트코드를 별도 파일로 담으므로 동적 링크에 가깝다고 보는 해석이
일반적이지만, LGPL의 "역공학 및 재링크 허용" 요건을 확실히 충족하려면 다음 중 하나를 택합니다.

1. **현행 유지 + 고지**: 소스가 공개되어 있으므로(이 저장소 자체가 오픈소스) 사용자가
   `pystray`를 교체·재빌드할 수 있다. 본 문서와 `packaging/build.ps1`이 그 경로를 제공.
2. **의존성 제거**: 트레이 UI를 표준 라이브러리(Tk) 기반으로 대체.

현재는 **1번**을 택하며, 저장소 공개와 빌드 스크립트 제공으로 재링크 가능성을 보장합니다.

## 5. 웹 UI

| 구성요소 | 용도 | 라이선스 | 배포 방식 |
|---|---|---|---|
| [@supabase/supabase-js](https://github.com/supabase/supabase-js) v2 | 인증, Realtime 구독, 조회 | MIT | jsDelivr CDN 참조(번들하지 않음) |
| WebCrypto API | 개인키 복원, DEK unwrap, payload 복호 | 브라우저 표준 | — |

웹 UI는 빌드 단계가 없는 정적 페이지이며 `node_modules` 의존성이 없습니다.

## 6. 컨테이너 이미지 (`periscribe-agent:latest`)

`periscribe-agent`가 사용자의 머신에서 **로컬로 빌드**하는 이미지입니다. 저장소는 Dockerfile
텍스트만 포함하며 이미지를 배포하지 않습니다.

| 레이어 | 라이선스 |
|---|---|
| `node:22-bookworm-slim` 베이스 | Debian 패키지 각각의 라이선스(주로 GPL/LGPL/MIT/BSD) + Node.js MIT |
| `git`, `ca-certificates`, `curl`, `ripgrep` (apt) | GPL-2.0 / MPL-2.0 / MIT·Apache-2.0 등 |
| `@anthropic-ai/claude-code` (npm) | Anthropic 상용 약관 |

## 7. 인용된 사양·프로토콜

| 대상 | 비고 |
|---|---|
| Anthropic Messages API | 프록시가 요청/응답 형식을 파싱. 공개 문서 기반, 코드 차용 없음 |
| Claude Code transcript JSONL 형식 | 관찰을 위한 형식 해석. 리버스 엔지니어링된 스키마가 아니라 공개 파일 구조 |
| Sysmon 이벤트 XML 스키마 | `wevtutil` 출력 파싱. Microsoft 공개 스키마 |

---

## 확인 방법

```bash
# Python 의존성 라이선스 일괄 확인
pip install pip-licenses
pip-licenses --from=mixed --format=markdown
```

## 갱신 이력

| 날짜 | 내용 |
|---|---|
| 2026-08 | 최초 작성. 대회 제출 전 라이선스 검증 대비 |
| 2026-08-18 | 검증: `pip-licenses` 로 실제 설치 메타데이터와 대조. 번들되는 전이 의존성 5건(§1.1) 추가 |
