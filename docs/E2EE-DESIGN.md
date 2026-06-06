# Periscribe — 종단간 암호화(E2EE) 설계 (구현됨)

> **목표**: periscribe를 호스팅 SaaS로 제공하면서, **운영자(중앙 DB 소유자)조차 테넌트 로그 내용을 못 읽게** 한다(*at-rest 제로지식*).
> 경쟁사는 오픈소스 자가호스팅으로 신뢰 문제를 회피하지만, 우리는 호스팅하면서 **하이브리드 공개키 암호화**로 보장한다.

핵심 결과:
- **collector 설치 = 토큰만**(패스프레이즈 입력·현장방문 없음). 머신이 per-device DEK를 로컬 생성해 owner 공개키로 봉인 업로드.
- **패스프레이즈는 웹(관리자)에서만**, 세션 단위.
- **운영자는 암호문 + 공개키 + 봉인 DEK만** 본다. per-device 키라 한 머신이 털려도 그 머신만.

---

## 0. 위협모델

| 적 | RLS만 | 본 설계 |
|---|:--:|:--:|
| 다른 테넌트(owner) | ✅ | ✅ |
| DB 백업/덤프 유출 | ❌ | ✅ |
| 운영자 / service_role 키 | ❌ | ✅(암호문만) |
| 법적 압수·DB 통째 제출 | ❌ | ✅(암호문만) |
| 머신 앞 사용자가 *자기* 로그 보는 것 | n/a | 막지 않음(로컬 transcript 평문 보유 — 애초에 대상 아님) |
| *악의적* 운영자가 웹 JS로 패스프레이즈 탈취 | ❌ | ⚠️ 못 막음(§7) |

**보장 경계**: 저장된 payload/raw 평문은 owner 패스프레이즈를 쥔 본인 외 누구도 못 봄. 메타데이터(kind/tool/ts/session_id/machine_id/device_id…)는 필터·인덱스용 평문 유지.

---

## 1. 키 계층 (하이브리드 공개키)

```
관리자 패스프레이즈 (사람만 앎, 네트워크로 절대 안 감)
   │ PBKDF2-SHA256(salt, 600k)
   ▼
  KEK (웹 메모리/세션에만)
   │ AES-256-GCM unwrap
   ▼
 owner 개인키 (RSA-OAEP 3072, 웹에서만 복원)   owner 공개키 (비밀 아님, 서버 평문)
   │ RSA-OAEP unwrap                                  │ RSA-OAEP wrap
   ▼                                                  ▼
 per-device DEK (각 머신 랜덤 32B) ◀──── collector가 생성, 공개키로 봉인해 업로드
   │ AES-256-GCM, row별 12B nonce
   ▼
 events.payload/raw 암호문 (envelope {v,kid,n,ct})
```

- **DEK 봉인**: RSA-OAEP-SHA256(소량 32B 봉인엔 ECIES보다 단순·안전, WebCrypto·`cryptography` 양쪽 네이티브).
- **payload 암호화**: AES-256-GCM. envelope `{v,kid,n,ct}`(base64), `events.enc_version=1`로 평문(0/null)과 구분.
- **개인키 봉인**: 패스프레이즈→PBKDF2(600k)→KEK→AES-256-GCM으로 PKCS8 봉인. 복구코드로 한 번 더 봉인(분실 대비).

서버 보관물(전부 비밀 아님): `owner_keys`(public_key, wrapped_private_key[+recovery], kdf_params{salt,iterations,recovery_salt}), `devices.wrapped_dek`.

---

## 2. 데이터 흐름

```
[ 테넌트 PC (머신마다) ]                       [ 운영자 Supabase (중앙) ]
 Claude Code → transcript.jsonl               ┌────────────────────────────────┐
        │ watch/parse                          │ owner_keys: pub + 봉인 개인키   │
        ▼                                      │ devices.wrapped_dek: 봉인 DEK    │
 Collector (설치=토큰만)                       │ events: 메타=평문, payload=암호문 │
  ├ per-device DEK 로컬 생성                    │ ingest(service_role): 평문 못 봄 │
  ├ DEK를 owner 공개키로 봉인→업로드           └───────────────┬────────────────┘
  └ payload AES-GCM 암호화 적재 ───────────────────────────────┤
                                                               │ Realtime + 조회(암호문)
                                                               ▼
                                                    [ Web UI (브라우저=관리자) ]
                                                     패스프레이즈→개인키 복원(세션)
                                                     →device별 DEK unwrap→payload 복호
```

**평문 키도 평문 payload도 중앙 서버를 절대 통과하지 않는다.**

---

## 3. 플로우

1. **웹 키 셋업(계정 1회)**: RSA 키쌍 생성 → 패스프레이즈로 개인키 봉인 + 복구코드 봉인 → `owner_keys` insert. 개인키는 세션 보관.
2. **웹 잠금해제(세션마다)**: `owner_keys` 조회 → 패스프레이즈로 개인키 복원 → `sessionStorage` 캐시(탭 닫으면 소멸).
3. **collector enroll(설치=토큰만)**: 하트비트 응답의 `enc.public_key` 수신 → per-device DEK 로컬 생성 → 공개키로 봉인해 하트비트로 업로드(`devices.wrapped_dek`) → 평문 DEK는 `config.json`에 보관. **공개키 수신 전(관리자 미설정)엔 적재 보류**(store-and-forward, 평문 전송 안 함).
4. **collector 암호화**: `sink.emit()`에서 payload/raw를 AES-GCM 암호화, `enc_version=1`.
5. **웹 복호화**: row의 `device_id`→`devices.wrapped_dek`→개인키로 unwrap→DEK 캐시→payload 복호. 잠금 시 🔒.
6. **ingest**: 응답에 `enc:{public_key,kid}` 동봉, `machine.wrapped_dek` 수령 저장. 비밀은 안 받음(제로지식).
7. **패스프레이즈 변경/분실**: 변경=개인키 re-wrap만. 분실=복구코드로 복원(둘 다 잃으면 영구 복호 불가).

---

## 4. 구현 위치

| 영역 | 파일 |
|---|---|
| 스키마 | `supabase/schema.sql` — `owner_keys`, `devices.wrapped_dek/dek_kid`, `events.enc_version` |
| collector 암호 | `collector/periscribe/crypto.py` (gen_dek/wrap_dek_rsa/encrypt_field), `config.py`(dek/dek_kid/encrypt), `sink.py`(emit 암호화+wrapped_dek 동봉), `collector.py`(공개키 수신·DEK 부트스트랩·적재 보류) |
| 의존성 | `collector/pyproject.toml` — `cryptography` |
| ingest | `supabase/functions/ingest/index.ts` — enc 배포 + wrapped_dek 수령 |
| 웹 | `web/app.js`(E2EE 모듈·키 셋업/잠금해제·복호화), `web/index.html`(enc 모달), `web/styles.css` |

---

## 5. 메타데이터 잔여 누출

운영자는 "어느 머신이 언제 어떤 도구를 써서 실패했는지"(kind/tool/ts/is_error/session_id/machine_id/device_id/project/cwd)까지는 본다. **명령·출력 내용(payload/raw)은 못 본다.** `project`/`cwd`는 sessions 뷰 드롭다운 의존으로 v1 평문 유지(더 가리려면 암호화+별칭).

---

## 6. 키 분실/회전

- **패스프레이즈 변경**: 웹에서 개인키 복원→새 KEK로 re-wrap→`wrapped_private_key`만 갱신. DEK·행 무변경.
- **분실**: 복구코드로 개인키 복원 후 새 패스프레이즈 재설정. 복구코드까지 잃으면 영구 복호 불가.
- **DEK는 per-device 독립**: 디바이스 추가=새 DEK, revoke=상호 무영향.
- **nonce**: 단일 DEK + 랜덤 96-bit nonce는 ~2³² 메시지 권장 한도. 대용량이면 디바이스/키 회전으로 분할.

---

## 7. 알려진 한계 — 웹 클라이언트 신뢰 (정직하게)

호스팅 SaaS에서 **복호화 웹 JS를 운영자가 서빙**한다.
- ✅ **at-rest 제로지식 성립**: 수동적 운영자·DB 덤프·service_role 탈취·법적 압수 — 전부 암호문.
- ⚠️ **능동적 악의 운영자**: 패스프레이즈를 빼돌리는 JS를 배포하면 복호화 시점에 탈취 가능(웹 E2EE의 구조적 한계).

**주장 가능**: "저장된 데이터는 운영자도 못 읽는다." **과장 금지**: "무슨 짓을 해도 절대"는 웹 클라이언트를 쓰는 한 별표.
**보강 로드맵**: 복호화 전용 데스크톱/CLI 뷰어, 웹 자산 SRI+버전 핀+서명/감사, 브라우저 확장 복호화.
