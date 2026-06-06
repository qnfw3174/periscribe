# Periscribe — 설계 흐름 문서 (E2EE / 디바이스 수명주기)

> 개발용 설계 문서. 컴포넌트 구조 · 핵심 로직(키/DEK) · 시나리오별 시퀀스 · 상태도 · 데이터 모델.
> 다이어그램은 [Mermaid](https://mermaid.js.org). GitHub·VS Code(Markdown Preview Mermaid)에서 렌더됨.
> 즉시 보려면 같은 폴더의 `design-flows.html`을 브라우저로 열면 됨.
>
> 범례: ✅ 구현됨(커밋 `e8924e5`) · 🟡 제안(미확정)

---

## 1. 컴포넌트 구조 (Context)

```mermaid
flowchart LR
  subgraph PC["① 테넌트 PC (머신마다 N대)"]
    CC["Claude Code"] -->|"transcript.jsonl (append-only)"| COL["Collector<br/>(읽기전용 watch)"]
    CFG[("config.json<br/>device_token · 평문 DEK")]
    COL <--> CFG
  end

  subgraph SB["② Supabase 중앙 (운영자 소유)"]
    ING["Edge Fn: ingest<br/>(service_role · 평문 못 봄)"]
    EV[("events<br/>meta=평문 / payload=암호문")]
    DV[("devices<br/>wrapped_dek")]
    OK[("owner_keys<br/>공개키 + 봉인 개인키")]
    ING --> EV
    ING --> DV
    ING -. "공개키 조회" .-> OK
  end

  subgraph WEB["③ 관리자 브라우저"]
    UI["Web UI<br/>(정적 + supabase-js)"]
  end

  COL ==>|"HTTPS: 암호문 + 봉인DEK<br/>device_token 인증"| ING
  ING -->|"enc.public_key · backfill"| COL
  UI -->|"REST + Realtime (암호문)"| EV
  UI -->|"owner_keys / devices 조회"| OK
  UI -. "anon + RLS(owner 스코핑)" .-> DV
```

핵심 원칙: **평문 키도 평문 payload도 중앙 서버를 통과하지 않는다.** 암호화는 Collector(쓰기)에서, 복호화는 브라우저(읽기)에서만.

---

## 2. 핵심 로직: 어떻게 DEK가 생기고, 공개키를 가져오나 ✅

per-device DEK는 **머신에서 랜덤 생성**되고, owner **공개키**는 **하트비트 응답**으로 받아온다. 패스프레이즈는 컬렉터에 전혀 안 들어간다.

```mermaid
sequenceDiagram
  autonumber
  participant COL as Collector
  participant ING as ingest (Edge Fn)
  participant DB as Postgres

  Note over COL: 설치 직후 config.dek="" · encrypt=true
  COL->>ING: beat() {device_token, machine}
  ING->>DB: devices 조회 (token_hash=sha256(token))
  ING->>DB: owner_keys.public_key 조회 (owner 기준)
  ING-->>COL: { enc:{public_key, kid}, backfill }

  alt public_key 없음 (관리자 미설정)
    Note over COL: 적재 보류 (store-and-forward)<br/>오프셋 전진 X · transcript 디스크 보존 · 다음 beat 재시도
  else public_key 수신
    COL->>COL: DEK = os.urandom(32)  ← 로컬 랜덤(패스프레이즈 불필요)
    COL->>COL: wrapped_dek = RSA-OAEP(public_key, DEK)
    COL->>COL: config.json 에 DEK·dek_kid 영속 (재시작 재사용)
    COL->>ING: 이후 beat/emit 마다 {machine.wrapped_dek, dek_kid} 동봉(자가치유)
    ING->>DB: devices.wrapped_dek = wrapped_dek
  end
```

> 코드: `collector/periscribe/collector.py:_handle_enc` · `crypto.py:gen_dek/wrap_dek_rsa` · `sink.py:_refresh_wrapped` · `functions/ingest/index.ts`

### 2.1 관리자 키 셋업 / 잠금해제 (웹) ✅

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web UI
  participant DB as Postgres

  alt owner_keys 없음 → 최초 셋업
    Admin->>WEB: 암호화 패스프레이즈 입력
    WEB->>WEB: RSA-OAEP 3072 키쌍 생성
    WEB->>WEB: KEK = PBKDF2(passphrase, salt, 600k)
    WEB->>WEB: wrapped_private_key = AES-GCM(KEK, 개인키)
    WEB->>WEB: 복구코드 생성 → wrapped_private_key_recovery
    WEB->>DB: owner_keys insert (공개키, 봉인 개인키, 복구본, kdf_params)
    WEB-->>Admin: 복구코드 1회 표시(저장 강제)
  else owner_keys 있음 → 잠금 해제
    Admin->>WEB: 패스프레이즈 입력
    WEB->>DB: owner_keys 조회
    WEB->>WEB: KEK 유도 → 개인키 unwrap (태그 실패=틀린 패스프레이즈)
    WEB->>WEB: 개인키를 sessionStorage 보관(탭 닫으면 소멸)
  end
```

> 코드: `web/app.js:encSetupFlow / encUnlockFlow / ensureEncUnlocked`

---

## 3. 시나리오별 흐름

### S1. 설치 → 로그 적재 → 조회 (정상 end-to-end) ✅

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web
  participant COL as Collector
  participant ING as ingest
  participant DB as Postgres

  Note over Admin,WEB: 선행 — 관리자 키 셋업(§2.1) 1회
  Admin->>WEB: 토큰 발급 (머신 관리)
  WEB->>DB: devices insert (token_hash)
  WEB-->>Admin: device_token (1회 표시)
  Admin->>COL: periscribe.exe 실행 + 토큰 입력 (설치=토큰만)

  Note over COL: DEK 부트스트랩(§2)
  COL->>ING: beat
  ING-->>COL: enc.public_key
  COL->>COL: DEK 생성 → 봉인 → config 저장

  loop 폴링 0.4s
    COL->>COL: 새 줄 파싱 → 이벤트
    COL->>COL: payload/raw 암호화(DEK) · enc_version=1 · kid
    COL->>ING: emit [암호문 이벤트] + wrapped_dek
    ING->>DB: events upsert(멱등) · devices 갱신
  end

  Note over Admin,WEB: 조회 (다른 시점)
  Admin->>WEB: 로그인 + 패스프레이즈
  WEB->>DB: owner_keys → 개인키 복원(세션)
  WEB->>DB: events 조회 + Realtime 구독 · devices.wrapped_dek 조회
  WEB->>WEB: 개인키로 device DEK unwrap → payload 복호
  WEB-->>Admin: 평문 로그 표시 ✅ (키 없으면 🔒)
```

### S2. 컬렉터 재시작 (DEK 유지) ✅

```mermaid
flowchart TD
  A["프로세스 시작"] --> B{"config.dek 있음?"}
  B -- "yes" --> C["sink.set_dek(config.dek)<br/>즉시 암호화 가능"]
  B -- "no" --> D["부트스트랩 대기(§2)"]
  C --> E["첫 beat에서 공개키 받아<br/>wrapped_dek 재계산·재업로드(자가치유)"]
  E --> F["정상 암호화 적재 루프"]
  D --> F
```

### S3. 암호화 키 대기 / 적재 보류 ✅

관리자가 아직 키를 안 만들었거나 네트워크 단절 시, **평문을 절대 올리지 않고** 보류한다.

```mermaid
flowchart TD
  A["폴링 루프 1회"] --> HB["heartbeat → enc.public_key 시도"]
  HB --> B{"encrypt=true?"}
  B -- "no(dry-run 등)" --> P["평문 적재"]
  B -- "yes" --> C{"DEK 보유?"}
  C -- "yes" --> E["암호화 적재"]
  C -- "no" --> F{"공개키 수신?"}
  F -- "no" --> H["적재 보류<br/>오프셋 유지 · last_error='키 대기'"] --> A
  F -- "yes" --> G["DEK 생성·봉인·저장(§2)"] --> E
  E --> A
  P --> A
```

### S4. Revoke (관리자가 차단) ✅

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web
  participant DB as Postgres
  participant COL as Collector
  participant ING as ingest

  Admin->>WEB: 머신 관리 → revoke
  WEB->>DB: devices.revoked = true
  loop 컬렉터 다음 호출
    COL->>ING: emit / beat
    ING->>DB: devices 조회 (revoked=true)
    ING-->>COL: 401 revoked
    COL->>COL: SinkAuthError → 지수 백오프(최대 300s)
  end
  Note over COL: 연속 10회 401 → 죽은 토큰 판단 → 자가 종료
  Note over WEB,DB: 헬스바에서 숨김(revoked) · 관리 모달엔 'revoked'로 표시<br/>events·wrapped_dek 유지 → 과거 로그 계속 복호 가능
```

### S5. Uninstall (제거 신호 + 로컬 정리) ✅

```mermaid
sequenceDiagram
  autonumber
  actor User as 머신 사용자
  participant BAT as uninstall.bat
  participant ING as ingest
  participant DB as Postgres

  User->>BAT: 실행
  BAT->>ING: POST {device_token, uninstall:true}
  ING->>DB: devices.uninstalled_at=now() · revoked=true
  ING-->>BAT: {ok, uninstalled:true}
  BAT->>BAT: HKCU Run 자동시작 해제
  BAT->>BAT: 실행 중 프로세스 종료(taskkill + CIM)
  BAT->>BAT: rmdir %LOCALAPPDATA%\Periscribe<br/>(config·평문DEK·checkpoint·log 삭제)
  Note over DB: 행은 남음("🗑 제거됨") · wrapped_dek 유지<br/>→ 과거 로그 복호 가능 · 사라지는 건 로컬 평문 DEK뿐
```

### S6. 디바이스 행 완전 삭제 (현재 동작) ✅ — ⚠ 데이터 접근 손실

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web
  participant DB as Postgres
  Admin->>WEB: 머신 관리 → 삭제(완전)
  WEB->>DB: devices delete (행 제거)
  Note over DB: wrapped_dek 함께 사라짐.<br/>events.device_id는 남지만(FK 없음) 봉인 DEK가 없어<br/>⚠ 그 머신의 암호 로그는 영구 복호 불가(🔒)
```

> 권장: E2EE에서 과거 로그를 보고 싶으면 **삭제 대신 revoke/제거됨 상태 유지**.

### S7. 재설치인데 machine_guid가 다를 때 (새 머신 취급) ✅

> 같은 머신이면 S8처럼 자동으로 한 디바이스로 합쳐진다. 아래는 **guid가 다른 경우**(OS 재설치/다른 PC/폴백 hostname 변경) — 새 디바이스로 잡힘.

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web
  participant COL2 as 재설치 Collector
  participant ING as ingest
  participant DB as Postgres

  Note over COL2: 클라이언트 삭제로 옛 config(평문 DEK) 소실
  Admin->>WEB: 새 토큰 발급
  WEB->>DB: devices insert (새 행 · 새 device_id)
  Admin->>COL2: 새 토큰 입력
  COL2->>ING: beat → enc.public_key
  COL2->>COL2: 새 DEK 생성·봉인
  COL2->>DB: 새 devices.wrapped_dek
  loop
    COL2->>DB: 새 events (새 device_id · 새 DEK)
  end
  Note over WEB,DB: 옛 행을 남겨두면 옛 로그도 복호 OK.<br/>단 관리 목록엔 디바이스 2개(연속성 X)
```

### S8. ✅ 디바이스 연속성 — machine_guid 자동 + DEK 키 히스토리

재설치해도 **같은 device 행**으로 자동 이어지고(관리자 매칭 불필요), 옛 로그도 계속 복호된다.
관리자는 **그냥 새 토큰 발급해서 그 머신에 꽂으면** 됨 — 같은 머신(machine_guid)이면 ingest가 알아서 합친다.

```mermaid
sequenceDiagram
  autonumber
  actor Admin as 관리자
  participant WEB as Web
  participant COL2 as 재설치 Collector
  participant ING as ingest
  participant DB as Postgres

  Note over COL2: 삭제로 옛 config(평문 DEK) 소실 · machine_guid는 OS라 그대로
  Admin->>WEB: 새 토큰 발급 (아무 거나)
  WEB->>DB: devices insert (토큰행 R2)
  Admin->>COL2: 새 토큰 입력 (같은 머신)
  COL2->>ING: beat {machine_guid=G, 토큰}
  ING->>DB: (owner,G) 기존 행 R1 발견 → R2 삭제, R1.token_hash=새토큰
  ING-->>COL2: enc.public_key
  COL2->>COL2: 새 DEK 세대 생성 (새 kid)
  COL2->>ING: wrapped_dek(새 kid)
  ING->>DB: R1.dek_keys[새 kid] = 봉인DEK  (누적, 덮어쓰기 X)
  loop
    COL2->>DB: 새 events (device_id=R1 · envelope.kid=새 kid)
  end
  Note over WEB,DB: 옛 이벤트(옛 kid)→dek_keys[옛 kid] 복호<br/>새 이벤트(새 kid)→dek_keys[새 kid] 복호<br/>관리 목록엔 디바이스 1개(연속) ✅ · revoke된 guid는 재설치해도 차단
```

식별자: `machine_guid` = Windows 레지스트리 **MachineGuid**(설치마다 고유, 앱 재설치에도 유지, 폴백 hostname).
구현: `devices.machine_guid`·`dek_keys`(kid→봉인DEK) · ingest가 (owner,guid)로 머지 + dek_keys 누적 · 웹 복호는 `dek_keys[envelope.kid]` · 컬렉터는 부트스트랩마다 랜덤 kid.

---

## 4. 디바이스 수명주기 상태도

```mermaid
stateDiagram-v2
  [*] --> issued: 토큰 발급
  issued: 발급됨(미설치)
  online: 온라인
  stale: 대기(last_seen 만료)
  revoked: revoked
  removed: 제거됨(uninstalled)

  issued --> online: 설치 + 하트비트
  online --> stale: 75s 무응답
  stale --> online: 하트비트 재개
  online --> revoked: 관리자 revoke
  stale --> revoked: 관리자 revoke
  online --> removed: uninstall 신호
  stale --> removed: uninstall 신호
  online --> online: 재설치(같은 machine_guid) → 같은 행 자동 연결
  revoked --> [*]: 완전 삭제(행 제거 · ⚠복호 손실)
  removed --> [*]: 완전 삭제
  note right of revoked: revoked/제거됨 guid는<br/>재설치해도 차단(되살리지 않음)
```

---

## 5. 데이터 모델 (ER)

```mermaid
erDiagram
  auth_users ||--o{ devices : "owns"
  auth_users ||--|| owner_keys : "has"
  auth_users ||--o{ events : "owns"
  devices ||--o{ events : "device_id(FK없음)"

  owner_keys {
    uuid owner_id PK
    text public_key "RSA 공개키(SPKI)"
    jsonb wrapped_private_key "KEK로 봉인한 개인키"
    jsonb wrapped_private_key_recovery "복구코드로 봉인"
    jsonb kdf_params "salt·iterations"
  }
  devices {
    uuid id PK
    uuid owner_id FK
    text token_hash "sha256(token)"
    bool revoked
    timestamptz uninstalled_at
    text wrapped_dek "owner 공개키로 봉인한 DEK"
    int dek_kid
  }
  events {
    text event_id PK "멱등키(transcript uuid)"
    uuid owner_id
    uuid device_id
    int enc_version "0=평문 / 1=암호문"
    jsonb payload "암호문 envelope {v,kid,n,ct}"
    jsonb raw
  }
```

---

## 6. 암호화 경계 요약

| 자산 | 위치 | 운영자 접근 |
|---|---|---|
| 평문 payload | Collector 메모리 · 브라우저(복호 후) | ❌ |
| 평문 DEK | 머신 `config.json` (로컬) | ❌ |
| 봉인 DEK | `devices.wrapped_dek` | ⭕(암호문) — 개인키 없으면 무용 |
| owner 개인키 | 브라우저(복원, 세션) / DB(봉인본) | ❌ / ⭕(봉인본) |
| owner 공개키 | DB · 응답 | ⭕(비밀 아님) |
| 패스프레이즈 | 사람 머릿속 | ❌ |
| 메타데이터 | `events`(평문) | ⭕ |

⚠ 한계: 복호화 웹 JS를 운영자가 서빙 → *악의적* 운영자의 복호화-시점 키 탈취는 못 막음(at-rest 제로지식까지가 보장 범위). 상세는 `docs/E2EE-DESIGN.md` §7.
