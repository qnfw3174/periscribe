# Docker · 컨테이너 · VS Code · Claude Code — 관계 정리

Periscribe에서 "에이전트를 컨테이너로 격리해 돌린다"가 실제로 어떤 부품들의 조합인지,
각자가 무슨 역할이고 무엇에 의존하는지를 정리한 개념 문서. 실무 절차는 [CONTAINERS.md](CONTAINERS.md),
배포는 [DEPLOY.md](DEPLOY.md) 참고.

---

## 0. 한 장 그림

```
        ┌─ 진입구(택1) ─────────────────────────────┐
        │  VS Code (Dev Containers 확장)             │   ← "리모컨 + UI" (선택)
        │  periscribe-agent  (우리 런처)             │   ← VS Code 대체, IDE 불필요
        └───────────────┬───────────────────────────┘
                        │  "이 설정으로 컨테이너 띄워줘" (= docker run)
                        ▼
              [ Docker Engine (dockerd) ]                ← 컨테이너를 *실제로 관리*하는 주체
                        │   이미지로 컨테이너 생성·실행·정지·삭제
                        ▼
        ┌─ 컨테이너 (격리된 리눅스 환경) ───────────┐
        │  Claude Code (CLI, 이미지에 npm 설치)      │   ← 실제 에이전트
        │   └ transcript → ~/.claude/projects        │
        │  마운트: 워크스페이스만 /workspace          │   ← 그 외 호스트 파일 안 보임(격리)
        │          --cap-drop=ALL, user=node          │
        └───────────────┬───────────────────────────┘
                        │  bind-mount 로 호스트에 흘러나옴
                        ▼
   [ 호스트 ] %USERPROFILE%\periscribe-agents\<name>\...\*.jsonl
                        │  호스트 Collector(periscribe.exe)가 watch → container_id 스탬프
                        ▼
   [ ingest 함수 → Supabase events ] → [ 웹: 🐳<name> 배지로 실시간 표시 ]
```

핵심 한 줄: **컨테이너의 주인은 Docker Engine이고, VS Code/`periscribe-agent`는 "띄워줘"라고
부탁하는 진입구일 뿐이며, Claude Code는 컨테이너 안에서 도는 평범한 CLI다.**

---

## 1. 부품별 역할

| 부품 | 정체 | 역할 | 없으면 |
|---|---|---|---|
| **Docker Engine** (`dockerd`) | 컨테이너 런타임(데몬) | 이미지 빌드, 컨테이너 생성·실행·정지·삭제, 볼륨/네트워크 관리. **컨테이너의 실제 관리자.** | 컨테이너 자체가 안 뜸 — **필수** |
| **Docker Desktop** | Engine + WSL2 VM + GUI 대시보드 묶음 | Windows/Mac에서 Engine을 쉽게 깔아주는 *편의 제품*. 라이선스 약관은 **여기**에만 걸림(§4). | Engine만 따로 깔면 됨 — Desktop 자체는 **선택** |
| **이미지(image)** | 컨테이너의 "설치 디스크 스냅샷" | OS+도구+claude-code가 구워진 읽기전용 템플릿. 컨테이너는 이미지로부터 인스턴스화됨. | — |
| **컨테이너(container)** | 이미지로 띄운 실행 인스턴스 | 격리된 리눅스 프로세스 공간. 여기서 Claude Code가 돎. | — |
| **VS Code + Dev Containers** | 에디터 + 확장 | 컨테이너를 띄우라 시키는 **런처** + 컨테이너 안 터미널/에디터를 보여주는 **UI**. | Claude·Docker 멀쩡 — **선택**(편한 진입구일 뿐) |
| **`periscribe-agent`** | 우리 CLI 런처(별도 exe) | VS Code 없이 `docker run -it`을 대신 호출해 컨테이너 안 Claude Code로 바로 진입. | VS Code로 진입하면 됨 — **택일** |
| **Claude Code** | 컨테이너 안 CLI 에이전트 | 실제 작업 수행 + transcript(`~/.claude/projects`) 기록. VS Code와 무관. | 에이전트가 없음 — **필수** |
| **Collector** (`periscribe.exe`) | 호스트 백그라운드 데몬 | bind로 흘러나온 transcript를 tail → Supabase 적재. **컨테이너 밖**, 호스트에서 돎. | 웹에 아무것도 안 뜸 — **필수** |

---

## 2. "누가 컨테이너를 관리하나" — Engine이지 VS Code가 아니다

- 대시보드/`docker ps`에 보이는 **컨테이너 ID는 Engine이 소유·실행**하는 진짜 컨테이너다.
- VS Code는 그걸 *만들어달라고 부탁한 손님*일 뿐 → **VS Code를 꺼도 컨테이너는 계속 산다.**
- 증거: VS Code가 띄운 컨테이너의 이미지 이름은 **`vsc-`** 로 시작한다(`vsc-<폴더>-<해시>`).
  `periscribe-agent`가 띄우면 `periscribe-agent:latest` 이미지를 쓰고 컨테이너명은 `periscribe-<name>`.

```
VS Code ─┐
docker CLI ─┼─▶ [ Docker Engine ] ─── 컨테이너(ID) 관리·실행
periscribe-agent ─┘        (진짜 관리자)     ↑ 대시보드는 이걸 "보여주는 창"일 뿐
```

---

## 3. 호스트 쉘 vs 컨테이너 쉘 — Claude Code는 "어느 무대"에서 도는가

같은 `claude` CLI라도 **어느 환경에서 실행되느냐**가 전부를 가른다.

| | 터미널(쉘)이 있는 곳 | Claude가 도는 곳 | transcript 위치 | 웹 표시 |
|---|---|---|---|---|
| **호스트에서 그냥 실행** | Windows(호스트) | 호스트 | 호스트 `~/.claude/projects` | 🖥 호스트 세션 |
| **컨테이너 안에서 실행** | 컨테이너(리눅스, node) | 컨테이너 | 컨테이너 `~/.claude/projects` → bind로 호스트 유출 | 🐳`<name>` |

- VS Code "Reopen in Container"가 하는 일은 단순 연결이 아니라 — **컨테이너를 띄우고,
  VS Code 백엔드(VS Code Server)를 그 안에 심어, 통합 터미널을 호스트 쉘에서 컨테이너 쉘로
  바꿔치기**하는 것. 그래서 그 터미널에서 친 `claude`는 컨테이너에서 돈다.
- `periscribe-agent`는 이 중 **VS Code Server를 심는 무거운 과정을 건너뛰고**, 그냥 처음부터
  컨테이너 안 쉘(또는 바로 `claude`)로 직접 진입시킨다 — 같은 무대(컨테이너), 다른 진입구(맨 터미널).

---

## 4. 두 가지 진입 방법 비교

| | VS Code "Reopen in Container" | **`periscribe-agent` (권장)** |
|---|---|---|
| 진입구 | IDE(에디터 창) | 터미널 한 줄 / exe 더블클릭 |
| 무게 | VS Code + Server를 컨테이너에 설치 | 없음(바로 `docker run -it`) |
| 띄우는 명령 | 확장이 `.devcontainer/`를 읽어 자동 | `periscribe-agent <폴더> --name <id>` |
| 인증 | 컨테이너 안에서 매번 별도 | 첫 1회 `/login` → 호스트에 영속(재로그인 불필요) |
| 컨테이너 이미지 | `vsc-…`(자동) | `periscribe-agent:latest`(임베드 Dockerfile로 1회 빌드) |
| 수집 | 동일 — bind → Collector → 🐳 | 동일 — bind → Collector → 🐳 |

두 방법 모두 **격리·수집 메커니즘은 동일**하다(워크스페이스만 마운트, `--cap-drop=ALL`,
경로 첫 세그먼트=`container_id`). 차이는 "어떻게 들어가느냐"뿐.

> `periscribe-agent`는 컨테이너의 `~/.claude` **전체**를 호스트 `<container_root>/<name>`에 bind한다
> → 로그인 토큰(`.credentials.json`)이 호스트에 남아 재실행 시 재로그인이 필요 없고, 그 아래
> `projects/...`의 transcript는 그대로 Collector가 수집한다. (devcontainer는 `~/.claude/projects`만 bind.)

---

## 5. 라이선스 — 걸리는 건 "컨테이너"가 아니라 "Docker Desktop"뿐

컨테이너 기술/엔진 자체는 무료다. 라이선스 약관은 **Docker Desktop이라는 제품**에만 적용된다.

**Docker Desktop (2026 기준):**

| 대상 | 비용 |
|---|---|
| 개인·학생·비상업 OSS·**직원 250명 미만 *그리고* 연매출 $1,000만 미만 소기업** | **무료** (Docker Personal) |
| 직원 250명 이상 **또는** 연매출 $1,000만 이상 | 유료(Pro/Team/Business, 인·월 $9~24) |

→ 개인/소규모면 Desktop도 **무료**. 큰 회사 PC에 배포할 때만 *그쪽*의 라이선스 부담이 생긴다.

**Desktop 라이선스를 피하면서 같은 컨테이너를 쓰는 무료 대안:**

| 런타임 | 라이선스 | 비고 |
|---|---|---|
| Docker Engine (WSL2에 직접 설치) | Apache 2.0 (무료) | Desktop 없이 `dockerd`만. 데몬 직접 관리(요즘 WSL2 systemd로 수월). |
| Podman / Podman Desktop | Apache 2.0 (무료) | 데몬리스·루트리스. `docker`와 CLI 호환. 상업 무료. |
| Rancher Desktop | 오픈소스 (무료) | 내부 moby/containerd. GUI도 무료. |

> Windows에서 "Engine만"은 결국 **WSL2 안의 `dockerd`**를 뜻한다(리눅스 커널 필요). 가능·무료지만
> 데몬을 직접 관리해야 한다.

**설계 방침:** `periscribe-agent`는 특정 런타임에 박지 않고 PATH의 `docker`(→ 향후 `podman`)를
자동 감지한다. 따라서 Desktop이든 Engine이든 Podman이든 **런처 코드 변경 없이** 동작한다 —
라이선스 결정을 미뤄도 되고, 나중에 갈아타도 공짜로 따라온다.

---

## 6. 용어 빠른 정리

- **이미지 vs 컨테이너**: 이미지=템플릿(읽기전용), 컨테이너=이미지로 띄운 실행 인스턴스. 컨테이너 여럿이 한 이미지를 공유.
- **bind mount vs volume**: bind=호스트의 *특정 경로*를 컨테이너에 직결(우리가 transcript 유출·로그인 영속에 사용). volume=Docker가 관리하는 익명/명명 저장소.
- **`--cap-drop=ALL`**: 컨테이너 프로세스의 리눅스 capability 전부 제거(권한 최소화).
- **`container_root`**: 호스트 Collector가 "컨테이너 세션"으로 인식해 watch하는 루트(`%USERPROFILE%\periscribe-agents`). 그 아래 **첫 경로 세그먼트 = `container_id`**(웹 🐳 라벨).

---

## 7. 에이전트 능력 제어 — 인프라 계층에서

`periscribe-agent`는 호스트의 **정책 JSON 파일**(`%LOCALAPPDATA%\Periscribe\policies\<name>.json`)을
읽어 `docker run` 플래그로 변환한다. 즉 제어가 **Claude의 권한 기능이 아니라 커널/Docker로 강제**되어
에이전트가 못 끈다(코드 수정·재빌드 없이 파일만 편집). 예: `workspace_writable:false`→코드 수정 차단,
`network:false`→외부 차단, `no_new_privileges`/리소스 제한. 키 표·예시는 [CONTAINERS.md](CONTAINERS.md)
"컨테이너 정책 파일".

## 8. 한계 (격리의 결)

- 이 구조의 격리는 **파일시스템 + capability + 네트워크/리소스(정책 파일)** 수준이다. 컨테이너는 호스트와
  **같은 커널**을 공유하므로 커널 익스플로잇까지 막으려면 microVM(예: Firecracker)이 필요하다.
- `network:false`는 외부를 통째로 끊는다(allowlist 기반 egress 프록시는 후속). denylist 한 겹은
  우회될 수 있으니 진짜 강제는 에이전트가 못 끄는 계층(위 정책 파일·네트워크 정책)에 둔다.

---

## 관련 문서
- [CONTAINERS.md](CONTAINERS.md) — devcontainer FS격리 실무 절차 + container_id 태깅 규약
- [DEPLOY.md](DEPLOY.md) — 멀티테넌트 배포(토큰/ingest/RLS)
- [E2EE-DESIGN.md](E2EE-DESIGN.md) — payload 암호화(운영자도 내용 못 봄)
