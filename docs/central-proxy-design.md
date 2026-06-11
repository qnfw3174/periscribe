# 설계: 중앙(remote) API 프록시 모드

> 상태: **설계 검토 완료, 미구현.** (2026-06-11)
> 결론: 기술적으로 가능. 기존 로컬 프록시 코드의 확장만으로 구현 가능하며, 컬렉터·ingest·웹은 무수정.
> ⚠ 이후 CLI 정리로 `proxy-setup`/`proxy-teardown` 커맨드는 제거됨(`proxy on|off`로 통합) —
> 본문의 `proxy-setup --remote ...`는 구현 시 `proxy on --remote ...` 형태로 읽을 것.

## 1. 배경과 목표

현재 API 프록시는 **대상 PC마다 로컬**(127.0.0.1:8077)로 떠서 Claude Code의 API 트래픽을
검사(차단/레닥션/주입)하고 spool에 로깅한다. 이 문서는 프록시를 **별도 서버 1대에 중앙으로
띄우고 여러 PC가 공유**하는 모드의 타당성 검토와 설계를 담는다.

결정사항:

| 항목 | 결정 |
|---|---|
| 검증 단계 | 본인 테스트 서버(VPS/사내 PC 1대)에서 기술 검증. 공개 CA 불요 |
| 로컬 모드 | **유지** — config/CLI로 local/remote 선택 (양쪽 지원) |
| 업로드 경로 | 중앙 서버에 **컬렉터를 같이 띄워** 기존 spool→암호화→ingest 파이프라인 재사용 |

## 2. 아키텍처

### 현재 (로컬 모드)

```
[PC마다]  Claude Code → 로컬 프록시(127.0.0.1:8077) → api.anthropic.com
                          └→ spool(_apilog/<machine>.jsonl) → 같은 PC 컬렉터 → ingest
```

### 제안 (remote 모드)

```
[PC ×N]   Claude Code ──TLS(자체 CA, 서버 SAN)──┐
            ANTHROPIC_BASE_URL =                │
            https://<server>:8077/m/<machine_key>
                                                ▼
[중앙 서버 1대]   프록시(0.0.0.0:8077) ──→ api.anthropic.com
                    │  요청 경로 /m/<key>/v1/... 에서 key 추출 → /v1/... 로 스트립해 중계
                    └→ spool(_apilog/<key>.jsonl, 머신별 분리)
                         └→ 서버 컬렉터 1개 (디바이스 토큰+DEK 1개) → ingest → Supabase
```

- PC에는 **프로세스 0개, 인증서 생성 0회**. settings.json env 2개만 기록
  (`ANTHROPIC_BASE_URL`, `NODE_EXTRA_CA_CERTS=<배포받은 ca.pem>`).
- 세션 식별(`metadata.user_id` → session_id)은 요청 본문 기반이라 무변경.

### 무수정으로 확인된 영역 (조사 근거)

| 영역 | 근거 |
|---|---|
| 컬렉터/parser | `parser.py:111-134` — 이벤트에 내장된 machine_id를 보존(setdefault). 서버 컬렉터 1개가 클라별 machine_id 그대로 적재 |
| ingest Edge Function | `ingest/index.ts:107` — owner/device만 스탬프, machine_id 패스스루 |
| 웹 대시보드 | `web/app.js:428` — 머신 필터는 적재된 이벤트의 machine_id 합집합으로 채워짐 |
| 세션 식별 | `apilog.py:32-50 session_id_for()` — 머신 무관 |

## 3. 핵심 설계 결정: 기기 식별 = URL 경로 프리픽스

중앙 프록시는 요청마다 "어느 PC에서 왔는지"를 알아야 올바른 machine_id를 스탬프한다.

| 후보 | 평가 |
|---|---|
| **(a) URL 경로 프리픽스 `/m/<key>` — 채택** | Claude Code가 base URL 경로 포함 지원(확정). env 2개 그대로, 프록시는 경로 스트립 1곳. 헬스 프로브도 base URL 기준 동작 |
| (b) ANTHROPIC_CUSTOM_HEADERS | 버전별 동작 편차, SDK 직접 사용 도구엔 미적용, 업스트림으로 헤더 안 새게 제거 필요 |
| (c) 소스 IP 매핑 | DHCP/NAT/VPN에서 IP 변동 → 오귀속. 매핑 테이블 운영 부담 |

- `<key>`는 sanitize(기존 `_proxy_spool_path`의 `re.sub(r"[^A-Za-z0-9_.-]+", ...)` 규칙 재사용)해서
  **그대로 machine_id로 사용** — 서버측 등록 파일 없는 zero-config. spool 파일명과 machine_id 일치.
- 프리픽스 없는 요청은 서버 config의 machine_id로 폴백 → **로컬 모드는 코드 경로가 사실상 동일, 회귀 없음**.
- **스푸핑**: 아무 클라이언트나 남의 key를 URL에 넣어 위장 가능. 테스트 단계는 허용.
  후속: key를 발급형 랜덤 토큰 + 서버측 매핑(machines.json, 미등록 403)으로 교체하면 식별+인증 겸용.

## 4. 신뢰 경계 / E2EE 제약 (중요)

중앙 프록시는 모든 클라이언트의 **평문 프롬프트·응답 + Anthropic API 키(x-api-key/Authorization
패스스루)** 를 보고 지나가게 한다. 암호화는 서버 컬렉터가 ingest 직전에만 적용하므로 서버 디스크의
spool(`_apilog/`)에는 평문이 머문다(적재 후에도 파일 잔존 — prune 정책 후속 검토).

→ 신뢰 경계가 "각 PC"에서 "프록시 서버"로 확장된다.

- 본인 테스트 단계: 문제 없음 (운영자=사용자).
- **고객 배포 시: 이 모드는 반드시 "고객사 사내망/고객 관리 서버에 설치"하는 형태여야 한다.**
  우리가 호스팅하는 중앙 프록시는 docs/E2EE-DESIGN.md의 제로지식("운영자도 내용 못 봄") 약속과
  **양립 불가**. 이 제약을 `proxy-serve` 기동 배너와 문서에 명기한다.

## 5. 컴포넌트별 변경 설계

### 5.1 `collector/periscribe/apiproxy.py` — 멀티테넌트화

- `MACHINE_PREFIX = "/m/"` 상수 + `split_machine_path(path) -> (machine_key|None, stripped_path)`
  순수함수 신설 (단위 테스트 용이).
- `_Ctx`: `machine_id` → `default_machine_id`, `spool_path` → `spool_dir`(디렉토리).
  `write_events(events, machine_id)`로 시그니처 변경, `spool_dir/<sanitize(key)>.jsonl`에 append
  (락은 기존 단일 락 유지 — 트래픽상 충분). 파일명 규칙이 기존과 동일하므로 컬렉터
  체크포인트(offsets) 보존 → 업그레이드 시 재적재 없음.
- `_Handler._proxy()`(84행~): 경로 스트립을 **헬스 비교(88행)보다 먼저** 수행해야
  `/m/<key>/__periscribe_health` 프로브가 동작. 이후 `is_messages` 판정(93행),
  업스트림 `conn.request(method, stripped_path, ...)`(137행) 모두 스트립된 경로 사용.
  `machine_id = sanitize(key) or ctx.default_machine_id`를 이벤트 생성·기록에 전달.
- `_make_server`/`run_proxy`(254/266행): `bind_host: str = "127.0.0.1"` 인자 추가
  (`proxy-serve`만 `0.0.0.0` 사용), `spool_path`→`spool_dir`.

### 5.2 `collector/periscribe/proxycert.py` — SAN 확장

- `ensure_certs(data_dir, extra_sans: list[str] | None = None)`: 항목별로
  `ipaddress.ip_address()` 시도 → 성공 시 `x509.IPAddress`, 실패 시 `x509.DNSName`.
- 기존 리프에 요청 SAN이 전부 포함됐는지 검사(`_leaf_has_sans` 신설), 부족하면
  **리프만 재발급(CA 재사용)** — 클라이언트들이 이미 신뢰 중인 ca.pem이 안 바뀌게.

### 5.3 `collector/periscribe/proxyguard.py` — remote URL 인식 (최대 함정)

`is_our_proxy_url()`(147-150행)이 `https://127.0.0.1|localhost` 프리픽스만 인식한다.
remote URL을 그대로 쓰면:
- `env_has_proxy()` 항상 False → `proxy status` 항상 "off"
- `strip_proxy_env()`(230행)가 "값이 우리 프록시가 아니면 그대로 둠" 분기로 no-op
  → **서버 장애 시 직결 복귀 불가 (lockout)**

수정:
- data_dir에 `proxy-remote.json` 마커(`{"base_url": "https://server:8077/m/key"}`)
  save/read/clear 함수 추가.
- `is_our_proxy_url()`: 로컬 프리픽스 매치 **또는** 마커 base_url과 일치 시 True.
- `health_probe()`(65행, "127.0.0.1" 하드코딩): `host="127.0.0.1"`, `path_prefix=""`
  파라미터 추가. 기존 호출부 무변경.

### 5.4 `collector/periscribe/config.py` — 키 3개 추가

```jsonc
{
  "api_proxy_mode": "local",        // "local" | "remote"
  "api_proxy_remote_url": "",       // https://<server>:8077 (경로 미포함)
  "api_proxy_machine_key": ""       // 비우면 sanitize(machine_id)
}
```

### 5.5 `collector/periscribe/__main__.py` — CLI

**신규 `proxy-serve` (서버측)**
```
periscribe proxy-serve --host <호스트명/IP, 복수 허용> [--port 8077] [-c config]
```
1. `ensure_certs(extra_sans=[hosts...])`
2. `run_proxy(default_machine_id=cfg.machine_id, bind_host="0.0.0.0", spool_dir=<watch_dir>/_apilog, ...)` 포그라운드 실행(콘솔 로그)
3. 기동 배너: ca.pem 경로, 클라이언트 셋업 명령 한 줄, E2EE 신뢰경계 경고

컬렉터는 별도 프로세스(`periscribe run`)로 동거 — 1단계에선 합치지 않음(변경 최소화).

**`proxy-setup --remote <url> --ca <ca.pem> [--key <key>]` (클라이언트측)**
1. 로컬 프로세스 기동·인증서 생성 전부 생략
2. `--ca` 파일을 `%LOCALAPPDATA%\Periscribe\remote-ca.pem`으로 복사
3. `base_url = <url>/m/<key>` (key 기본 = sanitize(cfg.machine_id))
4. 원격 헬스 프로브 **성공 시에만** `save_remote_base_url()` + `route_to_proxy()`
   — 기존 `_proxy_enable`의 lockout-safe 순서(548-565행) 유지
5. config에 mode/url/key + `api_log_enabled=true` 기록

**기존 커맨드 분기** (`_proxy_enable/_proxy_disable/_proxy_status`, 528/582/596행)
- `api_proxy_mode=="remote"`이면: enable=원격 재검증+재라우팅, disable=프로세스 기동/종료 생략
  + `strip_proxy_env()` + 마커 clear, status=`port_alive` 대신 원격 health_probe
  (urlsplit으로 host/port/prefix 파싱). on/degraded/off 판정 로직은 재사용.
- `proxy-gui`: 공유 함수가 분기를 처리하므로 무변경.
- `cmd_proxy_run`(511행): spool 인자를 디렉토리로 변경(새 시그니처 맞춤).

### 5.6 테스트 (`collector/tests/test_apiproxy.py`)

- `split_machine_path` 단위 테스트
- `/m/pc-a/v1/messages` → ① 업스트림에 `/v1/messages`로 전달 ② `_apilog/pc-a.jsonl`에
  machine_id="pc-a"로 스풀 ③ 프리픽스 없으면 default_machine_id 파일 ④ `/m/key/__periscribe_health` 200
- `_make_server` 시그니처 변경 반영

## 6. 운영 시나리오

### 서버 셋업 (1회)
1. 웹에서 디바이스 토큰 발급 → 서버에서 `periscribe setup`(대화형)으로 설치 (이름: proxy-server)
2. `periscribe proxy-serve --host <서버IP>` + `periscribe run -c config` (프로세스 2개)
3. `%LOCALAPPDATA%\Periscribe\ca.pem`을 클라이언트 PC들에 배포

### 클라이언트 셋업 (PC당 1회)
1. `periscribe proxy-setup --remote https://<서버IP>:8077 --ca ca.pem`
2. 떠 있는 Claude 세션은 최초 1회 재시작 (NODE_EXTRA_CA_CERTS는 Node 시작 시에만 읽힘 — 기존 규칙 동일)

### 장애 폴백 (SPOF)
- 중앙 프록시가 죽으면 **모든 PC**의 Claude가 영향받음 (로컬 모드는 1대만).
- 현행과 동일하게 수동 `proxy off` → settings.json 값 덮어쓰기 핫리로드로 실행 중 세션 포함 즉시 직결 복귀.
  (5.3 수정이 전제 — 안 고치면 복귀 불가)
- 후속: 클라이언트 경량 가디언(주기 헬스 프로브 → 유예 초과 시 자동 직결) 재활성화.
  proxyguard에 상수·히스테리시스가 이미 있어 구현 용이.

## 7. 검증 계획 (테스트 서버 1대 + 클라이언트 PC)

1. 서버 셋업 후 클라에서 `proxy-setup --remote ...` → 헬스 검증 성공 + env 기록 확인
2. 클라에서 Claude 한 턴 → 서버 `_apilog/<클라key>.jsonl` 생성 → 웹에서 source=api, machine=<클라key> 확인
3. `--key pc-b`로 둘째 키 셋업 → spool 분리 + 웹 머신 필터에 둘 다 표시
4. 정책: 서버 `proxy-policy.json`에 block 패턴 → 클라에서 403 + 차단 이벤트 기록
5. 폴백: 서버 프록시 kill → 클라 `proxy status`=degraded → `proxy off` → 직결 복귀
6. 회귀: 로컬 모드 PC에서 proxy on/off/status + `pytest collector/tests/test_apiproxy.py test_proxy_concurrency.py`

## 8. 한계와 후속 과제

| 항목 | 내용 |
|---|---|
| 디바이스 모니터링 | devices 목록·하트비트·last_seen은 서버 1대만 표시. 클라 PC별 생존 모니터링 없음 |
| SPOF | 중앙 프록시 장애 = 전 PC 영향. 자동 직결 가디언은 후속 |
| 스푸핑 | machine_key 위장 가능 → 발급형 토큰 + 서버측 매핑(미등록 403)으로 교체 |
| 정책 단위 | proxy-policy.json이 서버 1파일 전 머신 공통. per-machine 정책은 후속 |
| 평문 잔존 | 서버 spool에 평문 누적 — prune 정책 검토 |
| 제로지식 | 우리 호스팅 형태로는 제공 불가. 고객사 사내망 설치 전용 모드로 포지셔닝 |

## 9. 구현 순서 (착수 시)

1. `apiproxy.py` 멀티테넌트화 + `proxycert.py` SAN + 단위테스트 (로컬 회귀 없음 확인까지)
2. `proxyguard.py` remote 인식 + `config.py` 키
3. `__main__.py` `proxy-serve` / `proxy-setup --remote` / status·off 분기
4. 7절 end-to-end 검증
