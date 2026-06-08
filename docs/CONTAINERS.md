# Periscribe + 컨테이너 (파일시스템 격리)

`agent-container-isolation.md`의 §8을 가장 가볍게 구현한 것. **컨테이너=예방(위험한 능력을
안 줌), Periscribe=기록**이고, 이 문서는 둘을 잇는 마운트 규약 + `container_id` 태깅을 설명한다.

이번 범위는 **파일시스템 격리만**(워크스페이스 밖 호스트 파일을 컨테이너가 못 봄). 네트워킹/
egress 제한(stage 1)은 범위 밖이다.

> Docker·컨테이너·VS Code·Claude Code가 서로 어떤 관계인지(누가 컨테이너를 관리하나, 라이선스 등)
> 개념 정리는 [DOCKER-CONCEPTS.md](DOCKER-CONCEPTS.md). 아래 "VS Code Reopen in Container" 대신
> **VS Code 없이** 컨테이너에 진입하려면 `periscribe-agent` 런처를 쓴다(↓ "사용 B").

```
[ devcontainer ] Claude Code ─writes→ ~/.claude/projects (컨테이너)
   워크스페이스만 마운트       │ bind-mount
   (호스트 ~/.ssh 등 안 보임)  ▼
[ 호스트 ] %USERPROFILE%\periscribe-agents\<container_id>\<proj>\<session>.jsonl
            │ 호스트 Collector가 container_root 도 watch → 경로에서 container_id 추출 → 스탬프
            ▼ ingest 함수 → events(container_id)
[ 웹 ] 🐳 배지 + 컨테이너 필터로 호스트 vs 컨테이너 세션 구분
```

## 사전 준비 (이 PC: Windows 11 Home)
컨테이너 런타임이 없으면 설치가 필요하다(수 GB):
1. **WSL2**: 관리자 PowerShell에서 `wsl --install` → 재부팅.
2. **Docker Desktop**: 설치 후 *Settings → General → Use WSL 2 based engine* 켜기.
3. VS Code + **Dev Containers** 확장(devcontainer 열기용).

## 사용 A — VS Code (Reopen in Container)
1. 격리해서 돌릴 프로젝트 폴더에 이 레포의 `.devcontainer/`(Dockerfile + devcontainer.json)를 복사.
   - `devcontainer.json`의 마운트 규약상 **워크스페이스 폴더명이 container_id**가 된다.
2. VS Code에서 그 폴더 열기 → "Reopen in Container". 컨테이너 안에서 Claude Code 사용.
   - Claude Code 인증(ANTHROPIC_API_KEY 등)은 컨테이너 안에서 별도로.
3. 컨테이너의 transcript는 호스트 `%USERPROFILE%\periscribe-agents\<폴더명>\...`에 쌓인다.

## 사용 B — `periscribe-agent` 런처 (VS Code 없이, 권장)
VS Code/Dev Containers 확장 없이 컨테이너 안 Claude Code로 바로 진입한다. `.devcontainer/` 복사도 불필요
(런처가 Dockerfile을 임베드해 `periscribe-agent:latest` 이미지를 최초 1회 자동 빌드).
```
periscribe-agent <작업폴더> --name <box-id>
```
- `--name`(= container_id, 웹 🐳 라벨). 생략 시 작업 폴더명.
- 첫 실행이면 컨테이너 안에서 `/login` 한 번 → 호스트 `<container_root>/<name>\.credentials.json`에
  남아 **재실행 시 재로그인 불필요**(`~/.claude` 전체를 호스트에 bind하기 때문).
- `--shell` 로 claude 대신 bash 진입, `--api-key` 로 키 주입(로그인 대신).
- 마운트/격리/수집은 사용 A와 동일(워크스페이스만, `--cap-drop=ALL`, 경로 첫 세그먼트=container_id).
- 빌드: `packaging/build.ps1` → `periscribe-agent.exe`(컬렉터 `periscribe.exe`와 별도 console exe).
  개념·라이선스·런타임 대안은 [DOCKER-CONCEPTS.md](DOCKER-CONCEPTS.md).

### 컨테이너 정책 파일 (에이전트 능력 제어 — 인프라 강제)
컨테이너 안 에이전트가 무엇을 할 수 있는지를 **호스트의 정책 JSON 파일**로 제어한다. 런처가 이 파일을
읽어 `docker run` 플래그로 변환하므로 **커널/Docker가 강제**한다 — Claude의 권한 기능과 무관하고
에이전트가 못 끈다. **파일만 편집하면 다음 실행부터 적용(코드 수정·재빌드 불필요).**
- 위치: `%LOCALAPPDATA%\Periscribe\policies\<name>.json` (없으면 기본 템플릿 자동 생성) 또는 `--policy <경로>`.
- `--no-policy` 로 정책 미적용(기본 격리만).

| 키 | 기본 | 효과(docker) |
|---|---|---|
| `workspace_writable` | true | false → 워크스페이스 읽기전용 마운트(**코드 파일 수정 차단**) |
| `writable_paths` | [] | (ro 워크스페이스에서) 이 하위경로만 쓰기 허용 — 중첩 rw bind |
| `readonly_paths` | [] | (rw 워크스페이스에서) 이 하위경로만 보호(읽기전용) — 중첩 ro bind |
| `network` | true | false → `--network=none`(외부 차단). ⚠ Claude는 API/로그인 필요 → `--shell` 점검용 |
| `no_new_privileges` | false | true → `--security-opt=no-new-privileges`(권한 상승 차단) |
| `read_only_rootfs` | false | true → 루트FS 읽기전용(+`/tmp` tmpfs). 실험적 |
| `drop_all_capabilities` | true | base에서 항상 `--cap-drop=ALL`; false면 완화 |
| `memory`/`cpus`/`pids` | null | `--memory`/`--cpus`/`--pids-limit` 리소스 제한 |

예) "코드 읽되 수정 금지":
```json
{ "workspace_writable": false, "no_new_privileges": true }
```
예) "전체 읽기전용인데 `build/`·`out/`만 쓰기 허용":
```json
{ "workspace_writable": false, "writable_paths": ["build", "out"] }
```
예) "전체 쓰기 가능인데 `src/`·`.git/`만 보호":
```json
{ "workspace_writable": true, "readonly_paths": ["src", ".git"] }
```
하위경로 규칙은 워크스페이스 안에 실재하는 경로만 적용된다(밖이거나 없으면 경고 후 무시 — 보안).
잘못된 값/미지 키는 경고만 남기고 허용적 기본값으로 진행한다(launch 안 막힘).

## 호스트 Collector 설정
호스트 Collector가 컨테이너 루트도 보게 한다:
- `config.json`에 `"container_root": "%USERPROFILE%\\periscribe-agents"` (또는 절대경로),
  혹은 환경변수 `PERISCRIBE_CONTAINER_ROOT`.
- Collector는 `watch_dir`(native 세션)와 `container_root`(컨테이너 세션)를 **둘 다** 감시하고,
  컨테이너 루트의 파일은 경로 첫 세그먼트를 `container_id`로 스탬프한다.

## 웹에서 확인
- 컨테이너 세션 이벤트엔 **🐳 배지**가 붙는다.
- 상단 **컨테이너 필터**: `전체 / 🖥 호스트만 / 🐳 <id>` — 호스트 vs 컨테이너를 분리해 본다.

## 검수 포인트 (FS 격리 + 사각지대 없음)
- 컨테이너 안에서 워크스페이스 밖 호스트 파일(`~/.ssh/id_rsa`, 다른 레포, 시스템 경로) 접근 →
  **안 보임/불가** 이어야 한다(마운트 안 했으므로).
- 워크스페이스 안에서의 파일 삭제·수정은 호스트 워크스페이스에 반영됨(의도된 공유 — 에이전트가
  코드 작업해야 하니까). "보호"는 워크스페이스 **밖** 호스트 파일에 대한 것.
- 컨테이너 세션 활동이 웹에 🐳로 정상 표시되면 마운트 규약이 동작하는 것(= "샌드박스 안 활동이
  로그에서 통째로 사라지는 사각지대" 없음, 문서 §8 검수 포인트).

## 한계 (문서 §5)
- 이번은 **관찰 + 경로 태깅**이지 강제 차단이 아니다. denylist 한 겹은 우회될 수 있고(§5 Ona 사례),
  진짜 강제는 에이전트가 못 끄는 인프라 계층(egress 정책/네트워크 정책)에 둬야 한다 → 후속.
- OS 프로세스 샌드박스/컨테이너는 호스트와 같은 커널 — 커널 익스플로잇 방어가 필요하면 microVM.
